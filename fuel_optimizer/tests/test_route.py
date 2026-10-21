from unittest.mock import patch
from rest_framework.test import APITestCase
from rest_framework import status
from django.core.cache import cache
import requests


class RouteViewTests(APITestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_valid_route_request(self):
        def mock_requests_get(url, *args, **kwargs):
            class MockResponse:
                def __init__(self, json_data, status_code=200):
                    self._json = json_data
                    self.status_code = status_code

                def json(self):
                    return self._json

            if 'nominatim' in url or 'search' in url:
                query = kwargs.get('params', {}).get('q', '')
                if 'new york' in query.lower():
                    return MockResponse([{
                        'display_name': 'New York, United States',
                        'lat': '40.7128',
                        'lon': '-74.0060',
                        'address': {'country_code': 'us'}
                    }])
                elif 'philadelphia' in query.lower():
                    return MockResponse([{
                        'display_name': 'Philadelphia, PA, United States',
                        'lat': '39.9526',
                        'lon': '-75.1652',
                        'address': {'country_code': 'us'}
                    }])
            elif 'route' in url:
                return MockResponse({
                    'code': 'Ok',
                    'routes': [{
                        'distance': 152120.0,
                        'duration': 6432.0,
                        'geometry': {
                            'type': 'LineString',
                            'coordinates': [[-74.0060, 40.7128], [-75.1652, 39.9526]]
                        }
                    }]
                })
            return MockResponse([], 404)

        with patch('requests.get', side_effect=mock_requests_get):
            payload = {
                'start': 'New York, NY',
                'destination': 'Philadelphia, PA'
            }
            response = self.client.post('/api/route/', payload, format='json')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            data = response.json()

            self.assertEqual(data['start']['name'], 'New York, United States')
            self.assertEqual(data['start']['coordinates'], [-74.0060, 40.7128])
            self.assertEqual(data['destination']['name'], 'Philadelphia, PA, United States')
            self.assertEqual(data['destination']['coordinates'], [-75.1652, 39.9526])

            self.assertEqual(data['distance']['meters'], 152120.0)
            self.assertEqual(data['distance']['kilometers'], 152.12)
            self.assertEqual(data['distance']['miles'], 94.52)
            self.assertEqual(data['duration_seconds'], 6432.0)

            self.assertEqual(data['geometry']['type'], 'LineString')
            self.assertEqual(len(data['geometry']['coordinates']), 2)
            self.assertEqual(data['geometry']['coordinates'][0], [-74.0060, 40.7128])

            self.assertIn('routing_provider', data['metadata'])
            self.assertEqual(data['metadata']['vehicle_specs']['max_range_miles'], 500)
            self.assertEqual(data['metadata']['vehicle_specs']['mpg'], 10)

    def test_missing_start_field(self):
        response = self.client.post('/api/route/', {'destination': 'Philadelphia, PA'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        self.assertIn('error', data)
        self.assertIn('start', data['details'])

    def test_missing_destination_field(self):
        response = self.client.post('/api/route/', {'start': 'New York, NY'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        self.assertIn('error', data)
        self.assertIn('destination', data['details'])

    def test_blank_fields(self):
        response = self.client.post('/api/route/', {'start': '   ', 'destination': ''}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_location_not_found(self):
        class MockResponse:
            status_code = 200

            def json(self):
                return []

        with patch('requests.get', return_value=MockResponse()):
            response = self.client.post('/api/route/', {
                'start': 'NonExistentUnknownPlace12345',
                'destination': 'Philadelphia, PA'
            }, format='json')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            data = response.json()
            self.assertTrue(
                'could not be found' in data['error'].lower() or 'could not be resolved' in data['error'].lower()
            )

    def test_non_us_location_rejected(self):
        class MockResponse:
            status_code = 200

            def json(self):
                return [{
                    'display_name': 'Paris, France',
                    'lat': '48.8566',
                    'lon': '2.3522',
                    'address': {'country_code': 'fr'}
                }]

        with patch('requests.get', return_value=MockResponse()):
            response = self.client.post('/api/route/', {
                'start': 'Paris, France',
                'destination': 'Philadelphia, PA'
            }, format='json')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            data = response.json()
            self.assertTrue(
                'outside the usa' in data['error'].lower() or 'united states' in data['error'].lower()
            )

    def test_upstream_geocoding_timeout(self):
        with patch('requests.get', side_effect=requests.exceptions.Timeout("Connection timed out")):
            response = self.client.post('/api/route/', {
                'start': 'New York, NY',
                'destination': 'Philadelphia, PA'
            }, format='json')
            self.assertIn(response.status_code, (status.HTTP_502_BAD_GATEWAY, status.HTTP_503_SERVICE_UNAVAILABLE))
            data = response.json()
            self.assertIn('error', data)

    def test_upstream_routing_timeout(self):
        def mock_requests_get(url, *args, **kwargs):
            class MockResponse:
                status_code = 200

                def json(self):
                    return [{
                        'display_name': 'Dallas, TX',
                        'lat': '32.7767',
                        'lon': '-96.7970',
                        'address': {'country_code': 'us'}
                    }]

            if 'nominatim' in url or 'search' in url:
                return MockResponse()
            elif 'route' in url:
                raise requests.exceptions.Timeout("OSRM timeout")

        with patch('requests.get', side_effect=mock_requests_get):
            response = self.client.post('/api/route/', {
                'start': 'Dallas, TX',
                'destination': 'Houston, TX'
            }, format='json')
            self.assertIn(response.status_code, (status.HTTP_502_BAD_GATEWAY, status.HTTP_503_SERVICE_UNAVAILABLE))

    def test_route_caching_behavior(self):
        call_counts = {'nominatim': 0, 'osrm': 0}

        def mock_requests_get(url, *args, **kwargs):
            class MockResponse:
                def __init__(self, json_data, status_code=200):
                    self._json = json_data
                    self.status_code = status_code

                def json(self):
                    return self._json

            if 'nominatim' in url or 'search' in url:
                call_counts['nominatim'] += 1
                return MockResponse([{
                    'display_name': 'Sample Location, USA',
                    'lat': '35.0',
                    'lon': '-90.0',
                    'address': {'country_code': 'us'}
                }])
            elif 'route' in url:
                call_counts['osrm'] += 1
                return MockResponse({
                    'code': 'Ok',
                    'routes': [{
                        'distance': 100000.0,
                        'duration': 3600.0,
                        'geometry': {
                            'type': 'LineString',
                            'coordinates': [[-90.0, 35.0], [-90.0, 35.0]]
                        }
                    }]
                })
            return MockResponse([], 404)

        with patch('requests.get', side_effect=mock_requests_get):
            payload = {'start': 'City A, State', 'destination': 'City B, State'}

            # Request 1: cache miss
            resp1 = self.client.post('/api/route/', payload, format='json')
            self.assertEqual(resp1.status_code, status.HTTP_200_OK)
            data1 = resp1.json()
            self.assertFalse(data1['metadata']['cached']['route'])
            self.assertEqual(call_counts['nominatim'], 2)
            self.assertEqual(call_counts['osrm'], 1)

            # Request 2: should be served from cache
            resp2 = self.client.post('/api/route/', payload, format='json')
            self.assertEqual(resp2.status_code, status.HTTP_200_OK)
            data2 = resp2.json()
            self.assertTrue(data2['metadata']['cached']['route'])
            self.assertEqual(call_counts['nominatim'], 2)
            self.assertEqual(call_counts['osrm'], 1)
