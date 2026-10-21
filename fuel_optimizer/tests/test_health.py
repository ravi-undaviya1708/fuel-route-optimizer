from unittest.mock import patch
from rest_framework.test import APITestCase
from rest_framework import status
from fuel_optimizer.models import FuelStation


class HealthCheckTests(APITestCase):
    def test_health_check_healthy(self):
        FuelStation.objects.create(
            truckstop_id=99991,
            truckstop_name="Test Station",
            address="100 Main St",
            city="Dallas",
            state="TX",
            rack_id=101,
            retail_price=3.299,
            latitude=32.7767,
            longitude=-96.7970
        )

        response = self.client.get('/api/health/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['status'], 'healthy')
        self.assertEqual(data['service'], 'fuel-route-optimizer')
        self.assertTrue(data['database']['connected'])
        self.assertGreaterEqual(data['database']['total_fuel_stations'], 1)
        self.assertGreaterEqual(data['database']['stations_with_coordinates'], 1)

    def test_health_check_database_failure(self):
        with patch('django.db.connection.cursor') as mock_cursor:
            mock_cursor.side_effect = Exception("Database connection failed")
            response = self.client.get('/api/health/')
            self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
            data = response.json()
            self.assertEqual(data['status'], 'unhealthy')
            self.assertFalse(data['database']['connected'])
            self.assertIn("Database connection failed", data['database']['error'])
