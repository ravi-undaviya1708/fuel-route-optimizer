import os
import tempfile
from io import StringIO
from django.test import TestCase
from django.core.management import call_command
from django.core.management.base import CommandError
from fuel_optimizer.models import FuelStation


class ImportFuelPricesCommandTests(TestCase):
    def test_import_command_valid_csv(self):
        csv_content = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
1001,STATION ALPHA,100 HIGHWAY 1,Austin,TX,50,3.159
1002,STATION BETA,200 HIGHWAY 2,Dallas,TX,51,3.299
"""
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.csv') as f:
            f.write(csv_content)
            temp_path = f.name

        try:
            out = StringIO()
            call_command('import_fuel_prices', temp_path, stdout=out)
            output = out.getvalue()

            self.assertIn("Created new stations in DB: 2", output)
            self.assertEqual(FuelStation.objects.count(), 2)

            alpha = FuelStation.objects.get(truckstop_id=1001)
            self.assertEqual(alpha.truckstop_name, "STATION ALPHA")
            self.assertEqual(alpha.city, "Austin")
            self.assertEqual(alpha.state, "TX")
            self.assertEqual(str(alpha.retail_price), "3.15900000")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_import_command_invalid_prices(self):
        csv_content = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
2001,VALID STATION,100 MAIN,Denver,CO,10,3.50
2002,NEGATIVE PRICE,200 MAIN,Denver,CO,10,-1.50
2003,ZERO PRICE,300 MAIN,Denver,CO,10,0.00
2004,STRING PRICE,400 MAIN,Denver,CO,10,NOT_A_PRICE
"""
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.csv') as f:
            f.write(csv_content)
            temp_path = f.name

        try:
            out = StringIO()
            call_command('import_fuel_prices', temp_path, stdout=out)
            output = out.getvalue()

            self.assertIn("Created new stations in DB: 1", output)
            self.assertIn("Invalid rows rejected:      3", output)
            self.assertEqual(FuelStation.objects.count(), 1)
            self.assertTrue(FuelStation.objects.filter(truckstop_id=2001).exists())
            self.assertFalse(FuelStation.objects.filter(truckstop_id=2002).exists())
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_import_command_duplicates_and_idempotency(self):
        # 2 rows for station 3001 with differing prices
        csv_content = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
3001,PILOT 1,EXIT 10,Phoenix,AZ,80,3.899
3001,PILOT 1,EXIT 10,Phoenix,AZ,80,3.499
3002,LOVES 2,EXIT 20,Tucson,AZ,81,3.299
"""
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.csv') as f:
            f.write(csv_content)
            temp_path = f.name

        try:
            # First Run
            out1 = StringIO()
            call_command('import_fuel_prices', temp_path, stdout=out1)
            output1 = out1.getvalue()

            self.assertIn("Total CSV data rows read:    3", output1)
            self.assertIn("Unique stations in CSV:      2", output1)
            self.assertIn("Duplicate rows in CSV:       1", output1)
            self.assertIn("Created new stations in DB: 2", output1)
            self.assertEqual(FuelStation.objects.count(), 2)

            # Check lowest price was picked for station 3001
            s3001 = FuelStation.objects.get(truckstop_id=3001)
            self.assertEqual(str(s3001.retail_price), "3.49900000")

            # Second Run (Idempotency test)
            out2 = StringIO()
            call_command('import_fuel_prices', temp_path, stdout=out2)
            output2 = out2.getvalue()

            self.assertIn("Created new stations in DB: 0", output2)
            self.assertIn("Updated stations in DB:     0", output2)
            self.assertIn("Unchanged stations skipped: 2", output2)
            self.assertEqual(FuelStation.objects.count(), 2)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_import_command_missing_file(self):
        with self.assertRaises(CommandError) as ctx:
            call_command('import_fuel_prices', 'non_existent_file_path.csv')
        self.assertIn("CSV file not found", str(ctx.exception))

    def test_import_command_missing_required_headers(self):
        csv_content = """OPIS Truckstop ID,Some Other Column
1,Foo
"""
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.csv') as f:
            f.write(csv_content)
            temp_path = f.name

        try:
            with self.assertRaises(CommandError) as ctx:
                call_command('import_fuel_prices', temp_path)
            self.assertIn("CSV is missing required columns", str(ctx.exception))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
