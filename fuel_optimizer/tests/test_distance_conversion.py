from django.test import TestCase
from fuel_optimizer.services.routing import convert_meters_to_miles, convert_meters_to_kilometers


class DistanceConversionTests(TestCase):
    def test_distance_conversion_meters_to_miles(self):
        # 1609.344 meters is exactly 1 mile
        self.assertEqual(convert_meters_to_miles(1609.344), 1.0)
        self.assertEqual(convert_meters_to_miles(0.0), 0.0)
        self.assertEqual(convert_meters_to_miles(3218.688), 2.0)
        # 500 miles = 804672.0 meters
        self.assertEqual(convert_meters_to_miles(804672.0), 500.0)

    def test_distance_conversion_meters_to_kilometers(self):
        self.assertEqual(convert_meters_to_kilometers(1000.0), 1.0)
        self.assertEqual(convert_meters_to_kilometers(0.0), 0.0)
        self.assertEqual(convert_meters_to_kilometers(152120.0), 152.12)
