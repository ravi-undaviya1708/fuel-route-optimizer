from unittest.mock import patch
from django.test import TestCase
from fuel_optimizer.models import FuelStation
from fuel_optimizer.services.station_enrichment import StationEnrichmentService


class StationEnrichmentTests(TestCase):
    def test_enrich_from_dictionary(self):
        s1 = FuelStation.objects.create(
            truckstop_id=8001,
            truckstop_name="Station One",
            address="Address 1",
            city="City 1",
            state="TX",
            rack_id=1,
            retail_price=3.20,
        )
        s2 = FuelStation.objects.create(
            truckstop_id=8002,
            truckstop_name="Station Two",
            address="Address 2",
            city="City 2",
            state="TX",
            rack_id=1,
            retail_price=3.25,
        )

        coord_map = {
            8001: (32.7767, -96.7970),
            8002: (29.7604, -95.3698),
        }

        service = StationEnrichmentService()
        updated = service.enrich_from_dictionary(coord_map)

        self.assertEqual(updated, 2)
        s1.refresh_from_db()
        s2.refresh_from_db()
        self.assertEqual(s1.latitude, 32.7767)
        self.assertEqual(s1.longitude, -96.7970)
        self.assertEqual(s2.latitude, 29.7604)
        self.assertEqual(s2.longitude, -95.3698)

    def test_enrich_batch_via_census_mock(self):
        s1 = FuelStation.objects.create(
            truckstop_id=9001,
            truckstop_name="Station Census",
            address="100 Main St",
            city="Austin",
            state="TX",
            rack_id=1,
            retail_price=3.10,
        )

        # US Census batch geocoder response format:
        # "ID","InputAddress","MatchStatus","MatchType","MatchedAddress","Coordinates (lon,lat)","TigerLine","Side"
        mock_census_csv = '"9001","100 Main St, Austin, TX","Match","Exact","100 MAIN ST, AUSTIN, TX, 78701","-97.7431,30.2672","123456","L"\n'

        class MockCensusResponse:
            status_code = 200
            text = mock_census_csv

        with patch('requests.post', return_value=MockCensusResponse()):
            service = StationEnrichmentService()
            result = service.enrich_batch_via_census([s1])

            self.assertEqual(result['total'], 1)
            self.assertEqual(result['updated'], 1)
            self.assertEqual(result['unresolved'], 0)

            s1.refresh_from_db()
            self.assertEqual(s1.latitude, 30.2672)
            self.assertEqual(s1.longitude, -97.7431)
            self.assertTrue(s1.has_coordinates)
