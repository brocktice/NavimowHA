"""DataUpdateCoordinator for Navimow integration."""
import logging
import math
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from mower_sdk.api import MowerAPI
from mower_sdk.models import (
    Device,
    DeviceAttributesMessage,
    DeviceStateMessage,
    DeviceStatus,
)
from mower_sdk.sdk import NavimowSDK

from .const import (
    DOMAIN,
    HTTP_FALLBACK_MIN_INTERVAL,
    MQTT_STALE_SECONDS,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

PROBLEM_STATES = {"error", "unknown"}
PROBLEM_ERRORS = {"stuck", "lifted", "sensor_error", "motor_error", "blade_error"}
HEATMAP_STORE_VERSION = 1
HEATMAP_MAX_SAMPLES = 5000
HEATMAP_MAX_AGE = timedelta(days=45)
HEATMAP_MIN_SAMPLE_INTERVAL = timedelta(minutes=2)
HEATMAP_SAVE_DELAY = 5


class NavimowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Navimow data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        sdk: NavimowSDK,
        api: MowerAPI,
        device: Device,
        oauth_session: config_entry_oauth2_flow.OAuth2Session | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.sdk = sdk
        self.api = api
        self.device = device
        self.oauth_session = oauth_session
        self.data: dict[str, Any] = {}
        self._last_state: DeviceStateMessage | None = None
        self._last_attributes: DeviceAttributesMessage | None = None
        self._last_mqtt_update: float | None = None
        self._last_http_fetch: float | None = None
        self._last_data_source: str | None = None
        self._heatmap_save_task: asyncio.Task | None = None
        self._heatmap_samples: list[dict[str, Any]] = []
        self._last_heatmap_sample: dict[str, dict[str, Any]] = {}
        self._heatmap_store: Store[list[dict[str, Any]]] = Store(
            hass, HEATMAP_STORE_VERSION, f"{DOMAIN}_heatmap_{device.id}"
        )

    async def async_setup(self) -> None:
        """Register callbacks from SDK."""
        await self.async_load_heatmap()
        self.sdk.on_state(self._handle_state)
        self.sdk.on_attributes(self._handle_attributes)

    async def async_load_heatmap(self) -> None:
        """Load persisted mower heatmap samples."""
        loaded = await self._heatmap_store.async_load()
        if isinstance(loaded, list):
            self._heatmap_samples = self._pruned_heatmap_samples(loaded)
            self._last_heatmap_sample = {}
            for sample in self._heatmap_samples:
                mower_id = sample.get("mower_id")
                if isinstance(mower_id, str):
                    self._last_heatmap_sample[mower_id] = sample

    async def async_stop(self) -> None:
        """Persist pending heatmap data before unload."""
        if self._heatmap_save_task and not self._heatmap_save_task.done():
            self._heatmap_save_task.cancel()
            try:
                await self._heatmap_save_task
            except asyncio.CancelledError:
                pass
        await self._async_save_heatmap()

    def heatmap_samples(self) -> list[dict[str, Any]]:
        """Return heatmap samples for this mower."""
        return [
            sample
            for sample in self._heatmap_samples
            if sample.get("mower_id") == self.device.id
        ]

    def _build_data(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "state": self._last_state,
            "attributes": self._last_attributes,
            "meta": {
                "last_data_source": self._last_data_source,
                "last_mqtt_update_monotonic": self._last_mqtt_update,
                "last_http_fetch_monotonic": self._last_http_fetch,
            },
        }

    def _device_status_to_state(self, status: DeviceStatus) -> DeviceStateMessage:
        error: dict[str, Any] | None = None
        if status.error_code and status.error_code.value != "none":
            error = {
                "code": status.error_code.value,
                "message": status.error_message,
            }
        return DeviceStateMessage(
            device_id=status.device_id,
            timestamp=status.timestamp,
            state=status.status.value,
            battery=status.battery,
            signal_strength=status.signal_strength,
            position=status.position,
            error=error,
            metrics=None,
        )

    async def _async_ensure_valid_token(self) -> str | None:
        if not self.oauth_session:
            return None
        try:
            token: dict[str, Any] | None
            if hasattr(self.oauth_session, "async_ensure_token_valid"):
                await self.oauth_session.async_ensure_token_valid()
                token = self.oauth_session.token
            elif hasattr(self.oauth_session, "async_get_valid_token"):
                token = await self.oauth_session.async_get_valid_token()
            else:
                token = self.oauth_session.token
        except ConfigEntryAuthFailed:
            # 确定性认证失败（refresh_token 缺失或被服务端拒绝）→ 直接上报，让 HA 引导用户重新认证
            raise
        except Exception as err:
            # 瞬态错误（网络超时、DNS 等）→ 不立即触发重新认证流程。
            # 尝试沿用缓存中的 access_token；若缓存也不可用才升级为认证失败。
            _LOGGER.warning(
                "Token refresh failed (likely transient), falling back to cached token: %s", err
            )
            cached = getattr(self.oauth_session, "token", None)
            if cached and cached.get("access_token"):
                token = cached
            else:
                raise ConfigEntryAuthFailed(
                    f"Token refresh failed and no cached token available: {err}"
                ) from err
        if not token or not token.get("access_token"):
            raise ConfigEntryAuthFailed("No access token after refresh")
        access_token = token["access_token"]
        self.api.set_token(access_token)
        return access_token

    async def _async_update_data(self) -> dict[str, Any]:
        # 每次 update 都主动刷新 token，确保 api._token 与 oauth_session 保持同步。
        # 若仅在 HTTP fallback 时刷新，MQTT 正常推数据期间 token 长期不更新，
        # 过期后用户下发指令会立即收到 CODE_OAUTH_INFO_ILLEGAL。
        try:
            await self._async_ensure_valid_token()
        except ConfigEntryAuthFailed:
            raise

        cached_state = self.sdk.get_cached_state(self.device.id)
        if cached_state is not None:
            self._last_state = cached_state
            self._last_data_source = "mqtt_cache"
            self._maybe_record_heatmap_sample(cached_state)

        cached_attrs = self.sdk.get_cached_attributes(self.device.id)
        if cached_attrs is not None:
            self._last_attributes = cached_attrs

        now = time.monotonic()
        is_mqtt_stale = (
            self._last_mqtt_update is None
            or now - self._last_mqtt_update > MQTT_STALE_SECONDS
        )
        can_http_fetch = (
            self._last_http_fetch is None
            or now - self._last_http_fetch > HTTP_FALLBACK_MIN_INTERVAL
        )
        if is_mqtt_stale and can_http_fetch:
            try:
                status = await self.api.async_get_device_status(self.device.id)
                self._last_state = self._device_status_to_state(status)
                self._last_http_fetch = now
                self._last_data_source = "http_fallback"
                self._maybe_record_heatmap_sample(self._last_state)
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:
                _LOGGER.warning(
                    "HTTP fallback failed for device %s: %s", self.device.id, err
                )

        _LOGGER.debug(
            "Coordinator update: device=%s source=%s mqtt_ts=%s http_ts=%s",
            self.device.id,
            self._last_data_source,
            self._last_mqtt_update,
            self._last_http_fetch,
        )
        self.data = self._build_data()
        return self.data

    def _handle_state(self, state: DeviceStateMessage) -> None:
        if state.device_id != self.device.id:
            return
        _LOGGER.debug(
            "MQTT state received: device=%s state=%s battery=%s",
            state.device_id,
            state.state,
            state.battery,
        )
        self._last_mqtt_update = time.monotonic()
        self._last_data_source = "mqtt_push"
        self._maybe_record_heatmap_sample(state)
        self.hass.loop.call_soon_threadsafe(self._update_from_state, state)

    def _handle_attributes(self, attrs: DeviceAttributesMessage) -> None:
        if attrs.device_id != self.device.id:
            return
        _LOGGER.debug(
            "MQTT attributes received: device=%s keys=%d",
            attrs.device_id,
            len(getattr(attrs, "__dict__", {}) or {}),
        )
        self._last_mqtt_update = time.monotonic()
        self.hass.loop.call_soon_threadsafe(self._update_from_attributes, attrs)

    def _update_from_state(self, state: DeviceStateMessage) -> None:
        self._last_state = state
        self._last_data_source = "mqtt_push"
        self.async_set_updated_data(self._build_data())

    def _update_from_attributes(self, attrs: DeviceAttributesMessage) -> None:
        self._last_attributes = attrs
        self.async_set_updated_data(self._build_data())

    def get_device_state(self) -> DeviceStateMessage | None:
        return self.data.get("state")

    def get_device_attributes(self) -> DeviceAttributesMessage | None:
        return self.data.get("attributes")

    def get_device_info(self) -> Any | None:
        return self.data.get("device")

    def _maybe_record_heatmap_sample(self, state: DeviceStateMessage | None) -> None:
        """Persist a throttled position/status sample for heatmap rendering."""
        if state is None:
            return
        latitude, longitude = _extract_position(state.position)
        if latitude is None or longitude is None:
            return
        now = dt_util.utcnow()
        is_problem = _is_problem_state(state)
        last = self._last_heatmap_sample.get(self.device.id)
        if last and not _should_record_sample(last, now, latitude, longitude, is_problem):
            return

        sample = {
            "mower_id": self.device.id,
            "ts": now.isoformat(),
            "latitude": latitude,
            "longitude": longitude,
            "stuck": is_problem,
            "state": state.state,
            "error": state.error,
        }
        self._heatmap_samples.append(sample)
        self._last_heatmap_sample[self.device.id] = sample
        self._heatmap_samples = self._pruned_heatmap_samples(self._heatmap_samples)
        self._schedule_heatmap_save()

    def _pruned_heatmap_samples(
        self, samples: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Drop old or malformed heatmap samples."""
        cutoff = dt_util.utcnow() - HEATMAP_MAX_AGE
        pruned = [
            sample
            for sample in samples
            if _sample_datetime(sample) is not None
            and _sample_datetime(sample) >= cutoff
            and _coerce_float(sample.get("latitude")) is not None
            and _coerce_float(sample.get("longitude")) is not None
        ]
        return pruned[-HEATMAP_MAX_SAMPLES:]

    def _schedule_heatmap_save(self) -> None:
        """Debounce heatmap persistence."""
        if self._heatmap_save_task and not self._heatmap_save_task.done():
            return
        self._heatmap_save_task = self.hass.async_create_task(
            self._async_delayed_save_heatmap()
        )

    async def _async_delayed_save_heatmap(self) -> None:
        await asyncio.sleep(HEATMAP_SAVE_DELAY)
        await self._async_save_heatmap()

    async def _async_save_heatmap(self) -> None:
        await self._heatmap_store.async_save(self._heatmap_samples)


def _is_problem_state(state: DeviceStateMessage) -> bool:
    if state.state in PROBLEM_STATES:
        return True
    error = state.error
    if isinstance(error, dict):
        code = str(error.get("code") or error.get("error_code") or "").lower()
        return code in PROBLEM_ERRORS or "stuck" in code
    if isinstance(error, str):
        return error.lower() in PROBLEM_ERRORS or "stuck" in error.lower()
    return False


def _should_record_sample(
    last: dict[str, Any],
    now,
    latitude: float,
    longitude: float,
    is_stuck: bool,
) -> bool:
    """Return true when a heatmap sample is meaningfully new."""
    last_dt = _sample_datetime(last)
    if last_dt is None:
        return True
    if bool(last.get("stuck")) != is_stuck:
        return True
    if now - last_dt >= HEATMAP_MIN_SAMPLE_INTERVAL:
        return True
    last_lat = _coerce_float(last.get("latitude"))
    last_lon = _coerce_float(last.get("longitude"))
    if last_lat is None or last_lon is None:
        return True
    return _distance_m(last_lat, last_lon, latitude, longitude) >= 3


def _extract_position(position: Any) -> tuple[float | None, float | None]:
    if not isinstance(position, dict):
        return None, None
    latitude = _coerce_float(
        position.get("lat")
        or position.get("latitude")
        or position.get("y")
        or position.get("latGcj02")
        or position.get("latWgs84")
    )
    longitude = _coerce_float(
        position.get("lng")
        or position.get("lon")
        or position.get("longitude")
        or position.get("x")
        or position.get("lngGcj02")
        or position.get("lngWgs84")
    )
    if latitude is not None and longitude is not None:
        return latitude, longitude
    for key in ("gps", "location", "coordinate", "coordinates"):
        nested = position.get(key)
        if isinstance(nested, dict):
            latitude, longitude = _extract_position(nested)
            if latitude is not None and longitude is not None:
                return latitude, longitude
    return None, None


def _sample_datetime(sample: dict[str, Any]):
    value = sample.get("ts")
    if not isinstance(value, str):
        return None
    return dt_util.parse_datetime(value)


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    reference_lat = math.radians((lat1 + lat2) / 2)
    x = math.radians(lon2 - lon1) * 6371000 * math.cos(reference_lat)
    y = math.radians(lat2 - lat1) * 6371000
    return math.hypot(x, y)


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
