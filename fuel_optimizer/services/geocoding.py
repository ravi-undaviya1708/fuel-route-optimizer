import hashlib
import logging
import requests
from django.conf import settings
from django.core.cache import cache
from fuel_optimizer.exceptions import (
    LocationNotFoundError,
    NonUSLocationError,
    UpstreamServiceError,
    UpstreamServiceUnavailableError,
)

logger = logging.getLogger(__name__)


class NominatimGeocodingService:
    """
    Geocodes addresses within the United States using OpenStreetMap Nominatim.
    Adheres strictly to Nominatim's usage policy:
    - Custom descriptive User-Agent header
    - Respectful timeouts
    - Robust caching of resolved addresses
    - Restricts search to USA (countrycodes=us)
    """

    def __init__(
        self,
        base_url: str = None,
        user_agent: str = None,
        timeout: int = None,
        cache_ttl: int = None,
    ):
        self.base_url = (base_url or getattr(settings, 'NOMINATIM_BASE_URL', 'https://nominatim.openstreetmap.org')).rstrip('/')
        self.user_agent = user_agent or getattr(settings, 'NOMINATIM_USER_AGENT', 'FuelRouteOptimizer/1.0')
        self.timeout = timeout or getattr(settings, 'HTTP_TIMEOUT_SECONDS', 10)
        self.cache_ttl = cache_ttl or getattr(settings, 'CACHE_TTL_SECONDS', 86400)

    def _get_cache_key(self, query: str) -> str:
        normalized = ' '.join(query.strip().lower().split())
        query_hash = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
        return f"nominatim_geocode_{query_hash}"

    def geocode(self, query: str) -> dict:
        """
        Geocodes a US address or place name to [longitude, latitude] coordinates.

        Returns a dictionary:
        {
            "display_name": str,
            "latitude": float,
            "longitude": float,
            "coordinates": [lon, lat], # GeoJSON standard order
            "country_code": str,
            "cached": bool
        }

        Raises:
            LocationNotFoundError: If no matching location found
            NonUSLocationError: If location resolved outside USA
            UpstreamServiceError: If Nominatim returns non-200 HTTP code
            UpstreamServiceUnavailableError: On network timeout or connection failure
        """
        if not query or not query.strip():
            raise LocationNotFoundError("Location query cannot be blank.")

        clean_query = query.strip()
        cache_key = self._get_cache_key(clean_query)

        cached_result = cache.get(cache_key)
        if cached_result is not None:
            logger.info("Geocoding cache hit for query: '%s'", clean_query)
            result = dict(cached_result)
            result['cached'] = True
            return result

        endpoint = f"{self.base_url}/search"
        params = {
            'q': clean_query,
            'format': 'jsonv2',
            'countrycodes': 'us',
            'addressdetails': 1,
            'limit': 1,
        }
        headers = {
            'User-Agent': self.user_agent,
            'Accept': 'application/json',
        }

        logger.info("Requesting Nominatim geocoding for '%s'", clean_query)
        try:
            response = requests.get(
                endpoint,
                params=params,
                headers=headers,
                timeout=self.timeout
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            logger.error("Nominatim connection error for query '%s': %s", clean_query, exc)
            raise UpstreamServiceUnavailableError(
                f"Geocoding service is unavailable or timed out: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            logger.error("Nominatim request error for query '%s': %s", clean_query, exc)
            raise UpstreamServiceError(f"Geocoding service request failed: {exc}") from exc

        if response.status_code != 200:
            logger.error("Nominatim returned status %s for query '%s'", response.status_code, clean_query)
            raise UpstreamServiceError(
                f"Geocoding service returned HTTP status {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            logger.error("Failed to parse Nominatim JSON response: %s", exc)
            raise UpstreamServiceError("Invalid JSON response from geocoding service.") from exc

        if not data or not isinstance(data, list) or len(data) == 0:
            raise LocationNotFoundError(f"Address '{clean_query}' could not be resolved.")

        top_match = data[0]

        # Verify country code
        address_info = top_match.get('address', {})
        country_code = address_info.get('country_code', '').lower()
        if country_code and country_code not in ('us', 'usa'):
            raise NonUSLocationError(
                f"Location '{clean_query}' resolved outside the USA (country: {country_code.upper()})."
            )

        try:
            lat = float(top_match['lat'])
            lon = float(top_match['lon'])
        except (KeyError, ValueError) as exc:
            raise UpstreamServiceError("Geocoding response missing valid coordinates.") from exc

        # Basic bounding box sanity check for USA (including AK and HI)
        # Lat: ~18 to 72, Lon: ~-180 to -65
        if not (18.0 <= lat <= 72.0 and -180.0 <= lon <= -65.0):
            raise NonUSLocationError(
                f"Location '{clean_query}' coordinates ({lat}, {lon}) are outside the United States."
            )

        result = {
            'display_name': top_match.get('display_name', clean_query),
            'latitude': lat,
            'longitude': lon,
            'coordinates': [lon, lat],  # GeoJSON convention: [lon, lat]
            'country_code': country_code or 'us',
            'cached': False,
        }

        # Cache without the 'cached' flag
        cache_data = dict(result)
        cache.set(cache_key, cache_data, self.cache_ttl)

        return result
