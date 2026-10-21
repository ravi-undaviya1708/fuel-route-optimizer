from rest_framework.test import APITestCase
from rest_framework import status
from fuel_optimizer.models import FuelStation


class FuelStationListTests(APITestCase):
    def test_fuel_station_list_and_filters(self):
        FuelStation.objects.create(
            truckstop_id=5001,
            truckstop_name="Pilot Travel Center #10",
            address="Exit 15",
            city="Bakersfield",
            state="CA",
            rack_id=12,
            retail_price=4.599,
            latitude=35.3733,
            longitude=-119.0187
        )
        FuelStation.objects.create(
            truckstop_id=5002,
            truckstop_name="Loves Travel Stop #20",
            address="Exit 25",
            city="Dallas",
            state="TX",
            rack_id=15,
            retail_price=3.199,
            latitude=32.7767,
            longitude=-96.7970
        )

        # 1. Test basic list
        response = self.client.get('/api/fuel-stations/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['count'], 2)
        self.assertEqual(len(data['results']), 2)

        # 2. Test state filter
        response_ca = self.client.get('/api/fuel-stations/?state=CA')
        self.assertEqual(response_ca.status_code, status.HTTP_200_OK)
        data_ca = response_ca.json()
        self.assertEqual(data_ca['count'], 1)
        self.assertEqual(data_ca['results'][0]['state'], 'CA')

        # 3. Test search
        response_search = self.client.get('/api/fuel-stations/?search=Dallas')
        self.assertEqual(response_search.status_code, status.HTTP_200_OK)
        data_search = response_search.json()
        self.assertEqual(data_search['count'], 1)
        self.assertEqual(data_search['results'][0]['city'], 'Dallas')

        # 4. Test ordering by price
        response_order = self.client.get('/api/fuel-stations/?ordering=retail_price')
        self.assertEqual(response_order.status_code, status.HTTP_200_OK)
        data_order = response_order.json()
        self.assertEqual(data_order['results'][0]['truckstop_id'], 5002)  # Lower price first
