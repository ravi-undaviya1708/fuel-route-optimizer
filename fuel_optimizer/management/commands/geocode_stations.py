import os
import json
from django.core.management.base import BaseCommand
from fuel_optimizer.models import FuelStation
from fuel_optimizer.services.station_enrichment import StationEnrichmentService


class Command(BaseCommand):
    help = (
        "Enriches FuelStation records with latitude and longitude coordinates. "
        "Applies offline datasets first, then rate-conscious fallback geocoding."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size',
            type=int,
            default=2000,
            help="Number of stations per database batch operation (default: 2000)"
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help="Maximum number of stations to process in this run."
        )
        parser.add_argument(
            '--mapping-file',
            type=str,
            default=None,
            help="Optional path to a JSON file mapping truckstop_id -> [latitude, longitude]"
        )
        parser.add_argument(
            '--skip-online-fallback',
            action='store_true',
            help="Only run offline dataset matching; do not make online API fallback calls."
        )

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        limit = options['limit']
        mapping_file = options['mapping_file']
        skip_online = options['skip_online_fallback']

        enrichment_service = StationEnrichmentService()

        # 1. Custom mapping file if specified
        if mapping_file and os.path.exists(mapping_file):
            self.stdout.write(f"Loading custom coordinate mapping from: {mapping_file}")
            with open(mapping_file, 'r', encoding='utf-8') as f:
                raw_map = json.load(f)
            coord_map = {int(k): (v[0], v[1]) for k, v in raw_map.items()}
            updated = enrichment_service.enrich_from_dictionary(coord_map)
            self.stdout.write(self.style.SUCCESS(f"Updated {updated} stations from custom mapping file."))
            return

        total_stations = FuelStation.objects.count()
        init_unresolved = FuelStation.objects.filter(latitude__isnull=True).count()

        self.stdout.write(self.style.NOTICE("=" * 65))
        self.stdout.write(self.style.NOTICE("       FUEL STATION COORDINATE ENRICHMENT PROCESS"))
        self.stdout.write(self.style.NOTICE("=" * 65))
        self.stdout.write(f"Total stations in database:            {total_stations}")
        self.stdout.write(f"Stations currently without coordinates: {init_unresolved}")

        if init_unresolved == 0:
            self.stdout.write(self.style.SUCCESS("\nAll fuel stations already have coordinates! Nothing to do."))
            return

        # 2. Stage 1: Offline dataset matching
        self.stdout.write("\nStage 1: Running offline dataset matching (us_cities.csv + cache)...")
        offline_res = enrichment_service.enrich_from_offline_datasets(batch_size=batch_size)
        self.stdout.write(self.style.SUCCESS(
            f"  -> Enriched {offline_res['enriched']} stations instantly from offline datasets."
        ))

        remaining = FuelStation.objects.filter(latitude__isnull=True).count()
        self.stdout.write(f"  -> Remaining stations without coordinates: {remaining}")

        # 3. Stage 2: Fallback geocoding if any unresolved stations remain
        if remaining > 0 and not skip_online:
            self.stdout.write("\nStage 2: Running rate-conscious fallback geocoder for remaining stations...")
            online_res = enrichment_service.enrich_unresolved_via_geocoder(
                rate_limit_delay=1.0,
                max_retries=3,
                limit=limit
            )
            self.stdout.write(self.style.SUCCESS(
                f"  -> Online fallback resolved {online_res['resolved_locations']} locations "
                f"({online_res['stations_updated']} stations updated)."
            ))
            if online_res['failed'] > 0:
                self.stdout.write(self.style.WARNING(
                    f"  -> {online_res['failed']} locations could not be resolved."
                ))

        final_with_coords = FuelStation.objects.filter(latitude__isnull=False).count()
        final_unresolved = FuelStation.objects.filter(latitude__isnull=True).count()

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 65))
        self.stdout.write(self.style.SUCCESS("           COORDINATE ENRICHMENT SUMMARY"))
        self.stdout.write(self.style.SUCCESS("=" * 65))
        self.stdout.write(f"Total stations in DB:                  {total_stations}")
        self.stdout.write(self.style.SUCCESS(f"Stations now WITH coordinates:         {final_with_coords} ({final_with_coords/total_stations*100:.1f}%)"))
        self.stdout.write(f"Stations remaining for future retry:   {final_unresolved}")
        self.stdout.write(self.style.SUCCESS("=" * 65))
