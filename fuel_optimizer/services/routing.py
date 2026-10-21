import hashlib
import logging
import requests
from typing import Tuple, List, Dict, Any
from django.conf import settings
from django.core.cache import cache
from fuel_optimizer.exceptions import (
    UpstreamServiceError,
    UpstreamServiceUnavailableError,
)

logger = logging.getLogger(__name__)

# Standard conversion constants
METERS_PER_MILE = 1609.344
METERS_PER_KILOMETER = 1000.0


def convert_meters_to_miles(meters: float) -> float:
    """Converts distance in meters to miles rounded to 2 decimal places."""
    return round(meters / METERS_PER_MILE, 2)


def convert_meters_to_kilometers(meters: float) -> float:
    """Converts distance in meters to kilometers rounded to 2 decimal places."""
    return round(meters / METERS_PER_KILOMETER, 2)


class OSRMRouteService:
    """
    Fetches driving routes between two coordinates using Open Source Routing Machine (OSRM).
    Supports GeoJSON geometry extraction, road distance, duration, and response caching.
    """

    def __init__(
        self,
        base_url: str = None,
        timeout: int = None,
        cache_ttl: int = None,
    ):
        self.base_url = (base_url or getattr(settings, 'OSRM_BASE_URL', 'https://router.project-osrm.org')).rstrip('/')
        self.timeout = timeout or getattr(settings, 'HTTP_TIMEOUT_SECONDS', 10)
        self.cache_ttl = cache_ttl or getattr(settings, 'CACHE_TTL_SECONDS', 86400)

    def _get_cache_key(self, start_coords: Tuple[float, float], dest_coords: Tuple[float, float]) -> str:
        """
        Generates normalized cache key using 5 decimal places (~1.1 meter precision).
        Coordinates are (lon, lat).
        """
        lon1, lat1 = round(start_coords[0], 5), round(start_coords[1], 5)
        lon2, lat2 = round(dest_coords[0], 5), round(dest_coords[1], 5)
        raw_key = f"{lon1},{lat1};{lon2},{lat2}"
        key_hash = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()
        return f"osrm_route_{key_hash}"

    def get_route(
        self,
        start_coords: Tuple[float, float],
        dest_coords: Tuple[float, float],
    ) -> Dict[str, Any]:
        """
        Calculates driving route between start_coords (lon, lat) and dest_coords (lon, lat).

        Returns:
            Dict containing distance (miles, km, meters), duration, GeoJSON LineString geometry, and metadata.

        Raises:
            UpstreamServiceError: If OSRM returns an error code or invalid JSON
            UpstreamServiceUnavailableError: If OSRM times out or is unreachable
        """
        cache_key = self._get_cache_key(start_coords, dest_coords)
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            logger.info("Routing cache hit for key: %s", cache_key)
            result = dict(cached_data)
            result['cached'] = True
            return result

        lon1, lat1 = start_coords[0], start_coords[1]
        lon2, lat2 = dest_coords[0], dest_coords[1]

        # OSRM expects coordinates in {lon},{lat} format
        endpoint = f"{self.base_url}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
        params = {
            'overview': 'full',
            'geometries': 'geojson',
            'steps': 'false',
            'annotations': 'false',
        }
        headers = {
            'Accept': 'application/json',
            'User-Agent': getattr(settings, 'NOMINATIM_USER_AGENT', 'FuelRouteOptimizer/1.0'),
        }

        logger.info("Requesting OSRM route from (%s, %s) to (%s, %s)", lon1, lat1, lon2, lat2)
        try:
            response = requests.get(
                endpoint,
                params=params,
                headers=headers,
                timeout=self.timeout
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            logger.error("OSRM connection error: %s", exc)
            raise UpstreamServiceUnavailableError(
                f"OSRM routing service is unavailable or timed out: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            logger.error("OSRM request exception: %s", exc)
            raise UpstreamServiceError(f"Routing service request failed: {exc}") from exc

        if response.status_code != 200:
            logger.error("OSRM returned status %s: %s", response.status_code, response.text[:200])
            raise UpstreamServiceError(
                f"Routing service returned HTTP status {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            logger.error("Failed to parse OSRM JSON response: %s", exc)
            raise UpstreamServiceError("Invalid JSON response received from routing service.") from exc

        status_code = data.get('code')
        if status_code != 'Ok':
            msg = data.get('message', 'No driving route found between coordinates.')
            logger.warning("OSRM returned non-Ok code '%s': %s", status_code, msg)
            raise UpstreamServiceError(f"Route calculation failed: {msg}")

        routes = data.get('routes', [])
        if not routes:
            raise UpstreamServiceError("No route returned by routing service.")

        primary_route = routes[0]
        distance_meters = float(primary_route.get('distance', 0.0))
        duration_seconds = float(primary_route.get('duration', 0.0))
        geometry = primary_route.get('geometry')

        if not geometry or geometry.get('type') != 'LineString':
            raise UpstreamServiceError("Route did not return a valid LineString geometry.")

        result = {
            'distance_meters': round(distance_meters, 2),
            'distance_kilometers': convert_meters_to_kilometers(distance_meters),
            'distance_miles': convert_meters_to_miles(distance_meters),
            'duration_seconds': round(duration_seconds, 1),
            'duration_formatted': {
                'hours': int(duration_seconds // 3600),
                'minutes': int((duration_seconds % 3600) // 60),
                'seconds': int(duration_seconds % 60),
            },
            'geometry': geometry,
            'provider': 'OSRM (Open Source Routing Machine)',
            'cached': False,
        }

        # Cache without the 'cached' flag
        cache_data = dict(result)
        cache.set(cache_key, cache_data, self.cache_ttl)

        return result
