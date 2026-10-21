import csv
import os
import sys
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Optional

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from fuel_optimizer.models import FuelStation


class Command(BaseCommand):
    help = "Imports fuel prices and truckstop records from an OPIS CSV file."

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_file',
            nargs='?',
            default='data/fuel-prices-for-be-assessment.csv',
            type=str,
            help="Path to the fuel prices CSV file (default: data/fuel-prices-for-be-assessment.csv)"
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help="Clear all existing fuel station records before importing."
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=2000,
            help="Batch size for database bulk operations (default: 2000)"
        )

    def _normalize_header(self, header: str) -> str:
        """Normalizes header string by lowercasing and stripping whitespace."""
        h = header.strip().lower()
        if 'opis' in h or 'truckstop id' in h or h == 'id':
            return 'truckstop_id'
        if 'name' in h:
            return 'truckstop_name'
        if 'address' in h:
            return 'address'
        if 'city' in h:
            return 'city'
        if 'state' in h:
            return 'state'
        if 'rack' in h:
            return 'rack_id'
        if 'price' in h:
            return 'retail_price'
        return h

    def handle(self, *args, **options):
        csv_path = options['csv_file']
        clear_first = options['clear']
        batch_size = options['batch_size']

        if not os.path.exists(csv_path):
            raise CommandError(
                f"CSV file not found at '{csv_path}'. "
                f"Please ensure the file exists or place it under 'data/fuel-prices-for-be-assessment.csv'."
            )

        if clear_first:
            count, _ = FuelStation.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Cleared {count} existing FuelStation records."))

        self.stdout.write(f"Starting import from: {csv_path}")

        # Metrics counters
        total_rows = 0
        created_count = 0
        updated_count = 0
        skipped_count = 0
        invalid_count = 0
        invalid_details = []

        # Load existing stations into memory for fast lookup & comparison
        # Mapping truckstop_id -> FuelStation instance
        existing_stations: Dict[int, FuelStation] = {
            s.truckstop_id: s for s in FuelStation.objects.all()
        }

        # Staging structures
        to_create: list[FuelStation] = []
        to_update: list[FuelStation] = []
        duplicate_csv_rows = 0

        # Dictionary to hold the resolved record for each station from the CSV
        # Map: truckstop_id -> dict of normalized attributes
        csv_stations: Dict[int, Dict[str, Any]] = {}

        with open(csv_path, 'r', encoding='utf-8', errors='replace') as f:
            reader = csv.reader(f)
            try:
                raw_headers = next(reader)
            except StopIteration:
                self.stdout.write(self.style.ERROR("CSV file is empty."))
                return

            header_map = {idx: self._normalize_header(h) for idx, h in enumerate(raw_headers)}

            required_fields = {'truckstop_id', 'truckstop_name', 'address', 'city', 'state', 'rack_id', 'retail_price'}
            present_fields = set(header_map.values())
            missing_fields = required_fields - present_fields
            if missing_fields:
                raise CommandError(
                    f"CSV is missing required columns: {missing_fields}. "
                    f"Found columns: {raw_headers}"
                )

            for line_no, row in enumerate(reader, start=2):
                if not row or all(not cell.strip() for cell in row):
                    continue
                total_rows += 1

                # Parse row into dictionary
                row_dict = {}
                for idx, cell in enumerate(row):
                    if idx in header_map:
                        row_dict[header_map[idx]] = cell.strip()

                # 1. Validate truckstop_id
                raw_id = row_dict.get('truckstop_id', '')
                try:
                    truckstop_id = int(raw_id)
                except (ValueError, TypeError):
                    invalid_count += 1
                    invalid_details.append(f"Line {line_no}: Invalid truckstop ID '{raw_id}'")
                    continue

                # 2. Validate retail_price
                raw_price = row_dict.get('retail_price', '')
                try:
                    price = Decimal(raw_price)
                    if price <= Decimal('0'):
                        raise ValueError("Price must be greater than zero")
                except (InvalidOperation, ValueError, TypeError) as exc:
                    invalid_count += 1
                    invalid_details.append(f"Line {line_no} (ID {truckstop_id}): Invalid price '{raw_price}' ({exc})")
                    continue

                # 3. Validate rack_id
                raw_rack = row_dict.get('rack_id', '')
                try:
                    rack_id = int(raw_rack)
                except (ValueError, TypeError):
                    rack_id = 0

                # 4. Extract string fields
                name = row_dict.get('truckstop_name', '').strip()
                address = row_dict.get('address', '').strip()
                city = row_dict.get('city', '').strip()
                state = row_dict.get('state', '').strip().upper()

                if not name or not address:
                    invalid_count += 1
                    invalid_details.append(f"Line {line_no} (ID {truckstop_id}): Missing name or address")
                    continue

                # 5. Deterministic deduplication within the CSV file:
                # If station already seen in this CSV file, choose the lowest price for route optimization
                if truckstop_id in csv_stations:
                    existing_entry = csv_stations[truckstop_id]
                    if price < existing_entry['retail_price']:
                        csv_stations[truckstop_id] = {
                            'truckstop_name': name,
                            'address': address,
                            'city': city,
                            'state': state,
                            'rack_id': rack_id,
                            'retail_price': price,
                        }
                    # Count duplicate row in CSV
                    duplicate_csv_rows += 1
                else:
                    csv_stations[truckstop_id] = {
                        'truckstop_name': name,
                        'address': address,
                        'city': city,
                        'state': state,
                        'rack_id': rack_id,
                        'retail_price': price,
                    }

        # Compare normalized CSV stations against database
        for truckstop_id, data in csv_stations.items():
            if truckstop_id in existing_stations:
                existing = existing_stations[truckstop_id]
                if (existing.retail_price == data['retail_price'] and
                    existing.truckstop_name == data['truckstop_name'] and
                    existing.address == data['address'] and
                    existing.city == data['city'] and
                    existing.state == data['state'] and
                    existing.rack_id == data['rack_id']):
                    skipped_count += 1
                else:
                    existing.retail_price = data['retail_price']
                    existing.truckstop_name = data['truckstop_name']
                    existing.address = data['address']
                    existing.city = data['city']
                    existing.state = data['state']
                    existing.rack_id = data['rack_id']
                    to_update.append(existing)
            else:
                new_station = FuelStation(
                    truckstop_id=truckstop_id,
                    truckstop_name=data['truckstop_name'],
                    address=data['address'],
                    city=data['city'],
                    state=data['state'],
                    rack_id=data['rack_id'],
                    retail_price=data['retail_price'],
                )
                to_create.append(new_station)

        # Database transactions
        with transaction.atomic():
            if to_create:
                FuelStation.objects.bulk_create(to_create, batch_size=batch_size)
                created_count = len(to_create)

            if to_update:
                FuelStation.objects.bulk_update(
                    to_update,
                    fields=['truckstop_name', 'address', 'city', 'state', 'rack_id', 'retail_price', 'updated_at'],
                    batch_size=batch_size
                )
                updated_count = len(to_update)

        # Output summary report
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("           FUEL PRICES IMPORT SUMMARY"))
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(f"Total CSV data rows read:    {total_rows}")
        self.stdout.write(f"Unique stations in CSV:      {len(csv_stations)}")
        self.stdout.write(f"Duplicate rows in CSV:       {duplicate_csv_rows}")
        self.stdout.write(self.style.SUCCESS(f"  Created new stations in DB: {created_count}"))
        self.stdout.write(self.style.WARNING(f"  Updated stations in DB:     {updated_count}"))
        self.stdout.write(f"  Unchanged stations skipped: {skipped_count}")
        if invalid_count > 0:
            self.stdout.write(self.style.ERROR(f"  Invalid rows rejected:      {invalid_count}"))
            for err in invalid_details[:10]:
                self.stdout.write(self.style.ERROR(f"    - {err}"))
            if len(invalid_details) > 10:
                self.stdout.write(self.style.ERROR(f"    ... and {len(invalid_details) - 10} more"))
        else:
            self.stdout.write(f"  Invalid rows rejected:      {invalid_count}")
        self.stdout.write(f"Total stations now in DB:    {FuelStation.objects.count()}")
        self.stdout.write(self.style.SUCCESS("=" * 60))
