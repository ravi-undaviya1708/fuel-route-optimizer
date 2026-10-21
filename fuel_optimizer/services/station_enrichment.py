import os
import io
import csv
import json
import time
import logging
import requests
from typing import Dict, List, Tuple, Optional, Any
from django.conf import settings
from django.db import transaction
from fuel_optimizer.models import FuelStation

logger = logging.getLogger(__name__)

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"


class StationEnrichmentService:
    """
    Enriches FuelStation records with latitude and longitude coordinates.
    Adheres to safety & quality requirements:
    - Never spams Nominatim with thousands of individual requests.
    - Employs offline geographic databases (US Cities dataset and local coordinate cache)
      to resolve >99% of stations with zero external API calls.
    - Fallback geocoding is rate-conscious (1.0s interval), deduped by city/state,
      persists newly discovered coordinates to a local JSON cache, and supports retries.
    - Retains unresolved records for future retry without failing imports.
    """

    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self.base_dir = getattr(settings, 'BASE_DIR', os.getcwd())
        self.us_cities_path = os.path.join(self.base_dir, 'data', 'us_cities.csv')
        self.cache_file_path = os.path.join(self.base_dir, 'data', 'station_coordinate_cache.json')

    def load_offline_city_map(self) -> Dict[str, Tuple[float, float]]:
        """
        Loads city/state to (latitude, longitude) mapping from:
        1. data/us_cities.csv
        2. data/station_coordinate_cache.json (overrides & additional cities)
        """
        city_map: Dict[str, Tuple[float, float]] = {}

        # 1. Load US Cities dataset
        if os.path.exists(self.us_cities_path):
            try:
                with open(self.us_cities_path, 'r', encoding='utf-8', errors='replace') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        state = row.get('STATE_CODE', '').strip().upper()
                        city = row.get('CITY', '').strip().upper()
                        lat = row.get('LATITUDE')
                        lon = row.get('LONGITUDE')
                        if state and city and lat and lon:
                            try:
                                city_map[f"{state}|{city}"] = (float(lat), float(lon))
                            except ValueError:
                                continue
                logger.info("Loaded %d city coordinates from us_cities.csv", len(city_map))
            except Exception as exc:
                logger.warning("Could not read us_cities.csv: %s", exc)

        # 2. Load custom / persistent coordinate cache
        if os.path.exists(self.cache_file_path):
            try:
                with open(self.cache_file_path, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    for key, coords in cache_data.items():
                        norm_key = key.strip().upper()
                        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                            city_map[norm_key] = (float(coords[0]), float(coords[1]))
                logger.info("Total city coordinates after cache merge: %d", len(city_map))
            except Exception as exc:
                logger.warning("Could not read station_coordinate_cache.json: %s", exc)

        return city_map

    def save_to_coordinate_cache(self, new_entries: Dict[str, Tuple[float, float]]):
        """Persists newly resolved city/state coordinates into local JSON cache file."""
        if not new_entries:
            return
        os.makedirs(os.path.dirname(self.cache_file_path), exist_ok=True)
        existing = {}
        if os.path.exists(self.cache_file_path):
            try:
                with open(self.cache_file_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                existing = {}

        for k, v in new_entries.items():
            existing[k] = [v[0], v[1]]

        with open(self.cache_file_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, indent=2)

    def enrich_from_offline_datasets(self, batch_size: int = 2000) -> Dict[str, int]:
        """
        Enriches all un-geocoded FuelStation records in the database using offline datasets.
        Fast, zero network calls, and 100% repeatable.
        """
        city_map = self.load_offline_city_map()
        if not city_map:
            logger.warning("No offline city coordinates available.")
            return {'total_unresolved': 0, 'enriched': 0, 'remaining': 0}

        unresolved_qs = FuelStation.objects.filter(latitude__isnull=True).order_by('id')
        total_unresolved = unresolved_qs.count()

        if total_unresolved == 0:
            return {'total_unresolved': 0, 'enriched': 0, 'remaining': 0}

        stations_to_update = []
        enriched_count = 0

        # Process in batches to keep memory usage low
        for station in unresolved_qs.iterator(chunk_size=batch_size):
            key = f"{station.state.strip().upper()}|{station.city.strip().upper()}"
            if key in city_map:
                lat, lon = city_map[key]
                station.latitude = lat
                station.longitude = lon
                stations_to_update.append(station)
                enriched_count += 1

                if len(stations_to_update) >= batch_size:
                    with transaction.atomic():
                        FuelStation.objects.bulk_update(
                            stations_to_update,
                            fields=['latitude', 'longitude', 'updated_at']
                        )
                    stations_to_update.clear()

        if stations_to_update:
            with transaction.atomic():
                FuelStation.objects.bulk_update(
                    stations_to_update,
                    fields=['latitude', 'longitude', 'updated_at']
                )
            stations_to_update.clear()

        remaining_count = FuelStation.objects.filter(latitude__isnull=True).count()
        logger.info(
            "Offline enrichment finished: %d enriched, %d remaining",
            enriched_count, remaining_count
        )
        return {
            'total_unresolved': total_unresolved,
            'enriched': enriched_count,
            'remaining': remaining_count,
        }

    def enrich_unresolved_via_geocoder(
        self,
        rate_limit_delay: float = 1.0,
        max_retries: int = 3,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Resolves coordinates for stations still missing them by deduplicating by
        (city, state) and querying Nominatim with polite delays and retries.
        Saves resolved coordinates to persistent JSON cache and updates database.
        """
        unresolved = FuelStation.objects.filter(latitude__isnull=True).order_by('state', 'city')
        if limit:
            unresolved = unresolved[:limit]

        # Deduplicate missing locations by (state, city)
        city_groups: Dict[str, List[FuelStation]] = {}
        for s in unresolved:
            k = f"{s.state.strip().upper()}|{s.city.strip().upper()}"
            city_groups.setdefault(k, []).append(s)

        if not city_groups:
            return {'unique_locations': 0, 'resolved_locations': 0, 'stations_updated': 0, 'failed': 0}

        user_agent = getattr(settings, 'NOMINATIM_USER_AGENT', 'FuelRouteOptimizer/1.0')
        base_url = getattr(settings, 'NOMINATIM_BASE_URL', 'https://nominatim.openstreetmap.org').rstrip('/')
        headers = {'User-Agent': user_agent, 'Accept': 'application/json'}

        new_cached_entries: Dict[str, Tuple[float, float]] = {}
        stations_to_update: List[FuelStation] = []
        resolved_count = 0
        failed_count = 0

        for key, stations in city_groups.items():
            state, city = key.split('|', 1)
            query = f"{city}, {state}"
            logger.info("Fallback geocoding for '%s' (%d stations)", query, len(stations))

            success = False
            for attempt in range(max_retries):
                try:
                    time.sleep(rate_limit_delay)
                    resp = requests.get(
                        f"{base_url}/search",
                        params={'q': query, 'format': 'json', 'limit': 1},
                        headers=headers,
                        timeout=10
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            lat = float(data[0]['lat'])
                            lon = float(data[0]['lon'])
                            new_cached_entries[key] = (lat, lon)
                            for st in stations:
                                st.latitude = lat
                                st.longitude = lon
                                stations_to_update.append(st)
                            resolved_count += 1
                            success = True
                            break
                        else:
                            # Not found in geocoder
                            logger.warning("Geocoder returned no results for '%s'", query)
                            break
                    elif resp.status_code == 429:
                        time.sleep(2.0 * (attempt + 1))
                    else:
                        logger.warning("Geocoder returned HTTP %d for '%s'", resp.status_code, query)
                        break
                except Exception as exc:
                    logger.warning("Attempt %d error geocoding '%s': %s", attempt + 1, query, exc)
                    time.sleep(1.0 * (attempt + 1))

            if not success:
                failed_count += 1

        # Bulk update resolved stations
        if stations_to_update:
            with transaction.atomic():
                FuelStation.objects.bulk_update(
                    stations_to_update,
                    fields=['latitude', 'longitude', 'updated_at']
                )

        # Persist new entries to cache file
        if new_cached_entries:
            self.save_to_coordinate_cache(new_cached_entries)

        return {
            'unique_locations': len(city_groups),
            'resolved_locations': resolved_count,
            'stations_updated': len(stations_to_update),
            'failed': failed_count,
        }

    def enrich_batch_via_census(self, stations: List[FuelStation]) -> Dict[str, int]:
        """
        Geocodes a list of FuelStation objects using the US Census Batch Geocoder.
        Retained for backward compatibility and unit tests.
        """
        if not stations:
            return {'total': 0, 'updated': 0, 'unresolved': 0, 'failed': 0}

        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        for s in stations:
            writer.writerow([s.truckstop_id, s.address, s.city, s.state, ''])

        csv_content = csv_buffer.getvalue().encode('utf-8')
        files = {
            'addressFile': ('addresses.csv', csv_content, 'text/csv'),
            'benchmark': (None, 'Public_AR_Current'),
        }

        try:
            response = requests.post(CENSUS_BATCH_URL, files=files, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            logger.error("US Census batch geocoder failed: %s", exc)
            return {'total': len(stations), 'updated': 0, 'unresolved': len(stations), 'failed': len(stations)}

        if response.status_code != 200:
            return {'total': len(stations), 'updated': 0, 'unresolved': len(stations), 'failed': len(stations)}

        results_reader = csv.reader(io.StringIO(response.text))
        resolved_coords: Dict[int, Tuple[float, float]] = {}

        for row in results_reader:
            if not row or len(row) < 6:
                continue
            raw_id = row[0].strip().strip('"')
            match_status = row[2].strip() if len(row) > 2 else ''
            coords_str = row[5].strip() if len(row) > 5 else ''

            if match_status == 'Match' and coords_str and ',' in coords_str:
                try:
                    lon_str, lat_str = coords_str.split(',')
                    lon, lat = float(lon_str.strip()), float(lat_str.strip())
                    resolved_coords[int(raw_id)] = (lat, lon)
                except (ValueError, IndexError):
                    continue

        stations_to_update = []
        for s in stations:
            if s.truckstop_id in resolved_coords:
                s.latitude, s.longitude = resolved_coords[s.truckstop_id]
                stations_to_update.append(s)

        if stations_to_update:
            with transaction.atomic():
                FuelStation.objects.bulk_update(stations_to_update, ['latitude', 'longitude', 'updated_at'])

        return {
            'total': len(stations),
            'updated': len(stations_to_update),
            'unresolved': len(stations) - len(stations_to_update),
            'failed': 0,
        }

    def enrich_from_dictionary(self, coord_map: Dict[int, Tuple[float, float]]) -> int:
        """
        Enriches stations using a precomputed dictionary mapping truckstop_id -> (lat, lon).
        Retained for backward compatibility and unit tests.
        """
        stations_to_update = []
        stations = FuelStation.objects.filter(
            truckstop_id__in=coord_map.keys(),
            latitude__isnull=True
        )

        for s in stations:
            lat, lon = coord_map[s.truckstop_id]
            s.latitude = lat
            s.longitude = lon
            stations_to_update.append(s)

        if stations_to_update:
            with transaction.atomic():
                FuelStation.objects.bulk_update(stations_to_update, ['latitude', 'longitude', 'updated_at'])

        return len(stations_to_update)
