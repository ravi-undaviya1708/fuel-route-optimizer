from unittest.mock import patch
from django.test import TestCase
from rest_framework.test import APITestCase
from rest_framework import status
from fuel_optimizer.models import FuelStation
from fuel_optimizer.services.fuel_optimizer import (
    RouteFuelOptimizer,
    haversine_distance_miles,
    compute_calibrated_polyline_distances,
    find_stations_along_corridor,
)


class FuelOptimizerUnitTests(TestCase):
    def setUp(self):
        self.optimizer = RouteFuelOptimizer(max_range_miles=500.0, mpg=10.0)

    def test_haversine_distance(self):
        # Distance between (0, 0) and (0, 1) degrees latitude ~ 69 miles
        d = haversine_distance_miles(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(d, 69.0, delta=1.0)

        # Distance to self is 0
        self.assertEqual(haversine_distance_miles(-74.0, 40.7, -74.0, 40.7), 0.0)

    def test_route_shorter_than_500_miles_no_stops(self):
        """A route shorter than 500 miles requires no fuel stops when departing full."""
        candidate_stations = [
            {'truckstop_id': 1, 'route_mile': 150.0, 'retail_price': 3.20, 'coordinates': [-74.5, 40.5], 'distance_from_route_miles': 1.0}
        ]
        result = self.optimizer.optimize(total_road_miles=350.0, candidate_stations=candidate_stations)

        self.assertTrue(result['feasible'])
        self.assertFalse(result['requires_refueling'])
        self.assertEqual(result['total_stops'], 0)
        self.assertEqual(len(result['stops']), 0)
        self.assertEqual(result['total_fuel_gallons'], 0.0)
        self.assertEqual(result['total_gallons_purchased_at_stops'], 0.0)
        self.assertEqual(result['total_fuel_cost'], 0.0)
        self.assertEqual(result['estimated_trip_consumption_gallons'], 35.0)
        self.assertEqual(result['fuel_remaining_at_destination_gallons'], 15.0)
        self.assertEqual(result['final_leg_distance_miles'], 350.0)
        self.assertIn("No refueling", result['message'])

    def test_route_requiring_one_stop(self):
        """A route requiring exactly one stop tracks segment consumption and refills to full."""
        candidate_stations = [
            {
                'truckstop_id': 101,
                'truckstop_name': 'Station Expensive Early',
                'address': '100 Hwy',
                'city': 'Town A',
                'state': 'PA',
                'retail_price': 3.60,
                'coordinates': [-76.0, 40.5],
                'route_mile': 250.0,
                'distance_from_route_miles': 2.0,
            },
            {
                'truckstop_id': 102,
                'truckstop_name': 'Station Cheap Far',
                'address': '200 Hwy',
                'city': 'Town B',
                'state': 'OH',
                'retail_price': 2.99,
                'coordinates': [-82.0, 41.0],
                'route_mile': 420.0,
                'distance_from_route_miles': 1.5,
            },
            {
                'truckstop_id': 103,
                'truckstop_name': 'Station Expensive Late',
                'address': '300 Hwy',
                'city': 'Town C',
                'state': 'OH',
                'retail_price': 3.85,
                'coordinates': [-83.0, 41.2],
                'route_mile': 460.0,
                'distance_from_route_miles': 1.0,
            },
        ]

        result = self.optimizer.optimize(total_road_miles=800.0, candidate_stations=candidate_stations)

        self.assertTrue(result['feasible'])
        self.assertTrue(result['requires_refueling'])
        self.assertEqual(result['total_stops'], 1)

        stop = result['stops'][0]
        self.assertEqual(stop['truckstop_id'], 102)
        self.assertEqual(stop['retail_price'], 2.99)
        self.assertEqual(stop['distance_from_previous_leg_miles'], 420.0)
        self.assertEqual(stop['fuel_consumed_on_leg_gallons'], 42.0)
        self.assertEqual(stop['fuel_remaining_on_arrival_gallons'], 8.0)
        self.assertEqual(stop['estimated_gallons_purchased'], 42.0)
        self.assertEqual(stop['fuel_level_after_refueling_gallons'], 50.0)
        self.assertAlmostEqual(stop['estimated_cost'], 42.0 * 2.99, places=2)

        # Separate consumption and purchase accounting
        self.assertEqual(result['total_gallons_purchased_at_stops'], 42.0)
        self.assertEqual(result['total_fuel_gallons'], 42.0)
        self.assertEqual(result['estimated_trip_consumption_gallons'], 80.0)
        self.assertEqual(result['final_leg_distance_miles'], 380.0)
        self.assertEqual(result['final_leg_fuel_consumed_gallons'], 38.0)
        self.assertEqual(result['fuel_remaining_at_destination_gallons'], 12.0)
        self.assertAlmostEqual(result['total_fuel_cost'], round(42.0 * 2.99, 2), places=2)

    def test_route_requiring_multiple_stops(self):
        """A 1450-mile route requiring multiple stops validates fuel state on each leg."""
        candidate_stations = [
            {
                'truckstop_id': 301,
                'truckstop_name': 'Stop 1',
                'retail_price': 3.10,
                'coordinates': [-78.0, 40.0],
                'route_mile': 450.0,
                'distance_from_route_miles': 1.0,
            },
            {
                'truckstop_id': 302,
                'truckstop_name': 'Stop 2',
                'retail_price': 3.20,
                'coordinates': [-84.0, 41.0],
                'route_mile': 900.0,
                'distance_from_route_miles': 1.0,
            },
            {
                'truckstop_id': 303,
                'truckstop_name': 'Stop 3',
                'retail_price': 3.05,
                'coordinates': [-90.0, 41.5],
                'route_mile': 1300.0,
                'distance_from_route_miles': 1.0,
            },
        ]

        result = self.optimizer.optimize(total_road_miles=1450.0, candidate_stations=candidate_stations)

        self.assertTrue(result['feasible'])
        self.assertEqual(result['total_stops'], 3)

        # Check that purchase never exceeds 50 gallons on any stop
        for stop in result['stops']:
            self.assertLessEqual(stop['estimated_gallons_purchased'], 50.0)
            self.assertGreater(stop['estimated_gallons_purchased'], 0.0)
            self.assertAlmostEqual(
                stop['estimated_cost'],
                round(stop['estimated_gallons_purchased'] * stop['retail_price'], 2),
                places=2
            )
            self.assertLessEqual(stop['distance_from_previous_leg_miles'], 500.0)

        # Check final leg
        self.assertLessEqual(result['final_leg_distance_miles'], 500.0)
        self.assertEqual(result['final_leg_distance_miles'], 150.0)

        # Trip fuel conservation: initial (50) + purchased = trip consumption + destination remaining
        total_purchased = result['total_gallons_purchased_at_stops']
        trip_consumed = result['estimated_trip_consumption_gallons']
        remaining_dest = result['fuel_remaining_at_destination_gallons']
        self.assertAlmostEqual(50.0 + total_purchased, trip_consumed + remaining_dest, places=2)

    def test_partially_empty_tank_refilled(self):
        """Departing with a partially empty tank calculates correct arrival fuel and purchase quantity."""
        candidate_stations = [
            {
                'truckstop_id': 401,
                'truckstop_name': 'Early Stop',
                'retail_price': 3.40,
                'coordinates': [-75.0, 40.0],
                'route_mile': 120.0,
                'distance_from_route_miles': 1.0,
            }
        ]

        # Vehicle departs with only 15.0 gallons (150 miles range) on a 350-mile trip
        result = self.optimizer.optimize(
            total_road_miles=350.0,
            candidate_stations=candidate_stations,
            initial_fuel_gallons=15.0
        )

        self.assertTrue(result['feasible'])
        self.assertEqual(result['total_stops'], 1)

        stop = result['stops'][0]
        self.assertEqual(stop['distance_from_previous_leg_miles'], 120.0)
        self.assertEqual(stop['fuel_consumed_on_leg_gallons'], 12.0)
        self.assertEqual(stop['fuel_remaining_on_arrival_gallons'], 3.0)  # 15.0 - 12.0 = 3.0
        self.assertEqual(stop['estimated_gallons_purchased'], 47.0)      # 50.0 - 3.0 = 47.0
        self.assertLessEqual(stop['estimated_gallons_purchased'], 50.0)
        self.assertAlmostEqual(stop['estimated_cost'], round(47.0 * 3.40, 2), places=2)

        # Final leg: 350 - 120 = 230 miles. Consumes 23.0 gal. Remaining = 50 - 23 = 27.0 gal.
        self.assertEqual(result['final_leg_distance_miles'], 230.0)
        self.assertEqual(result['fuel_remaining_at_destination_gallons'], 27.0)

    def test_purchase_never_exceeds_tank_capacity(self):
        """Purchase quantity strictly never exceeds the 50-gallon tank capacity even on near-empty arrivals."""
        candidate_stations = [
            {
                'truckstop_id': 501,
                'truckstop_name': 'Near Empty Stop',
                'retail_price': 3.15,
                'coordinates': [-79.0, 40.0],
                'route_mile': 490.0,
                'distance_from_route_miles': 1.0,
            }
        ]

        result = self.optimizer.optimize(total_road_miles=750.0, candidate_stations=candidate_stations)

        self.assertTrue(result['feasible'])
        stop = result['stops'][0]
        self.assertEqual(stop['fuel_consumed_on_leg_gallons'], 49.0)
        self.assertEqual(stop['fuel_remaining_on_arrival_gallons'], 1.0)
        self.assertEqual(stop['estimated_gallons_purchased'], 49.0)
        self.assertLessEqual(stop['estimated_gallons_purchased'], 50.0)

    def test_stop_cost_equals_purchased_gallons_times_price(self):
        """Each stop cost must precisely equal purchased gallons multiplied by that stop's price."""
        candidate_stations = [
            {
                'truckstop_id': 601,
                'truckstop_name': 'Precise Pricing Stop',
                'retail_price': 3.2567,
                'coordinates': [-80.0, 40.0],
                'route_mile': 380.0,
                'distance_from_route_miles': 1.0,
            }
        ]

        result = self.optimizer.optimize(total_road_miles=650.0, candidate_stations=candidate_stations)

        self.assertTrue(result['feasible'])
        stop = result['stops'][0]
        expected_cost = round(stop['estimated_gallons_purchased'] * 3.2567, 2)
        self.assertEqual(stop['estimated_cost'], expected_cost)
        self.assertEqual(result['total_fuel_cost'], expected_cost)

    def test_separation_of_consumption_and_purchases(self):
        """Trip fuel consumption and fuel purchases at stops are reported in distinct fields."""
        candidate_stations = [
            {
                'truckstop_id': 701,
                'truckstop_name': 'Midway Stop',
                'retail_price': 3.00,
                'coordinates': [-80.0, 40.0],
                'route_mile': 400.0,
                'distance_from_route_miles': 1.0,
            }
        ]

        result = self.optimizer.optimize(total_road_miles=700.0, candidate_stations=candidate_stations)

        # 700 miles @ 10 MPG = 70.0 gallons consumed over entire trip
        self.assertEqual(result['estimated_trip_consumption_gallons'], 70.0)
        # But at stop 1 (mile 400), only 40.0 gallons were purchased
        self.assertEqual(result['total_gallons_purchased_at_stops'], 40.0)
        self.assertEqual(result['total_fuel_gallons'], 40.0)
        # Remaining at destination = 50 - (300 / 10) = 20.0 gallons
        self.assertEqual(result['fuel_remaining_at_destination_gallons'], 20.0)

    def test_every_route_leg_is_within_vehicle_range(self):
        """All legs (start to stop, between stops, and to destination) must be within 500 miles."""
        candidate_stations = [
            {
                'truckstop_id': 801,
                'retail_price': 3.10,
                'coordinates': [-78.0, 40.0],
                'route_mile': 450.0,
                'distance_from_route_miles': 1.0,
            },
            {
                'truckstop_id': 802,
                'retail_price': 3.20,
                'coordinates': [-84.0, 40.0],
                'route_mile': 920.0,
                'distance_from_route_miles': 1.0,
            }
        ]

        result = self.optimizer.optimize(total_road_miles=1300.0, candidate_stations=candidate_stations)

        self.assertTrue(result['feasible'])
        leg1 = result['stops'][0]['distance_from_previous_leg_miles']
        leg2 = result['stops'][1]['distance_from_previous_leg_miles']
        final_leg = result['final_leg_distance_miles']

        self.assertLessEqual(leg1, 500.0)
        self.assertLessEqual(leg2, 500.0)
        self.assertLessEqual(final_leg, 500.0)

    def test_infeasible_route_gap_exceeds_max_range(self):
        """A route with a gap exceeding 500 miles returns feasible: False and clear message."""
        candidate_stations = [
            {
                'truckstop_id': 201,
                'truckstop_name': 'Last Hope Station',
                'address': 'Hwy 1',
                'city': 'Desert Edge',
                'state': 'NV',
                'retail_price': 3.50,
                'coordinates': [-115.0, 36.0],
                'route_mile': 300.0,
                'distance_from_route_miles': 1.0,
            },
            {
                'truckstop_id': 202,
                'truckstop_name': 'Oasis Station',
                'address': 'Hwy 1',
                'city': 'Oasis',
                'state': 'UT',
                'retail_price': 3.40,
                'coordinates': [-110.0, 38.0],
                'route_mile': 850.0,
                'distance_from_route_miles': 1.0,
            }
        ]

        result = self.optimizer.optimize(total_road_miles=1200.0, candidate_stations=candidate_stations)

        self.assertFalse(result['feasible'])
        self.assertTrue(result['requires_refueling'])
        self.assertIn("Infeasible route", result['message'])
        self.assertIn("exceeds the 500-mile", result['message'])

    def test_missing_or_invalid_station_prices_and_coordinates(self):
        """Stations with null or invalid prices/coordinates are safely ignored."""
        candidate_stations = [
            # Invalid retail_price
            {'truckstop_id': 901, 'route_mile': 200.0, 'retail_price': None, 'coordinates': [-76.0, 40.0]},
            {'truckstop_id': 902, 'route_mile': 250.0, 'retail_price': -2.50, 'coordinates': [-77.0, 40.0]},
            {'truckstop_id': 903, 'route_mile': 300.0, 'retail_price': 0.0, 'coordinates': [-78.0, 40.0]},
            # Invalid coordinates
            {'truckstop_id': 904, 'route_mile': 350.0, 'retail_price': 3.20, 'coordinates': None},
            {'truckstop_id': 905, 'route_mile': 380.0, 'retail_price': 3.20, 'coordinates': [None, None]},
            # Valid station
            {'truckstop_id': 906, 'route_mile': 420.0, 'retail_price': 3.10, 'coordinates': [-80.0, 40.0], 'distance_from_route_miles': 1.0},
        ]

        result = self.optimizer.optimize(total_road_miles=750.0, candidate_stations=candidate_stations)

        self.assertTrue(result['feasible'])
        self.assertEqual(result['total_stops'], 1)
        self.assertEqual(result['stops'][0]['truckstop_id'], 906)
        self.assertEqual(result['stops'][0]['retail_price'], 3.10)

    def test_new_york_to_miami_multi_stop_regression(self):
        """
        Regression test for New York, NY -> Miami, FL route (1,279.55 miles).
        Validates:
        1. Distinguishes perpendicular offset (2.76 mi, 1.05 mi) from actual road detours.
        2. Station 1 road detour: 17.48 mi to, 5.65 mi from (23.13 mi total).
        3. Station 2 road detour: 3.36 mi to, 7.26 mi from (10.62 mi total).
        4. Every driving leg (Leg 1: 400.37 mi, Leg 2: 413.92 mi, Final Leg: 499.01 mi) <= 500.0 miles.
        5. Total driving distance = 1,313.30 miles (1,279.55 main + 33.75 detour).
        6. Precise fuel accounting: 81.43 gal purchased, $242.57 total cost, 131.33 gal consumed.
        7. Initial 50.0-gallon tank cost is excluded.
        """
        total_road_miles = 1279.55
        candidate_stations = [
            {
                'truckstop_id': 61804,
                'truckstop_name': 'CARTERS ONE STOP',
                'address': '26615 S CRATER RD',
                'city': 'Petersburg',
                'state': 'VA',
                'retail_price': 2.959,
                'coordinates': [-77.420845, 37.227447],
                'projection_coordinates': [-77.458925, 37.199464],
                'route_mile': 382.89,
                'distance_from_route_miles': 2.76,  # Perpendicular offset
                'detour_to_station_miles': 17.48,   # Actual driving distance to station
                'detour_from_station_miles': 5.65,  # Actual driving distance from station back to highway
                'detour_distance_miles': 23.13,
            },
            {
                'truckstop_id': 71283,
                'truckstop_name': 'QUIKTRIP #7135',
                'address': '173 YEMASSEE HWY',
                'city': 'Yemassee',
                'state': 'SC',
                'retail_price': 2.999,
                'coordinates': [-80.686566, 32.350395],
                'projection_coordinates': [-80.704285, 32.352494],
                'route_mile': 787.80,
                'distance_from_route_miles': 1.05,  # Perpendicular offset
                'detour_to_station_miles': 3.36,    # Actual driving distance to station
                'detour_from_station_miles': 7.26,  # Actual driving distance from station back to highway
                'detour_distance_miles': 10.62,
            }
        ]

        result = self.optimizer.optimize(total_road_miles=total_road_miles, candidate_stations=candidate_stations)

        self.assertTrue(result['feasible'])
        self.assertTrue(result['requires_refueling'])
        self.assertEqual(result['total_stops'], 2)

        # Distinguish main-route mileage from additional station-detour mileage
        self.assertEqual(result['main_route_distance_miles'], 1279.55)
        self.assertEqual(result['total_detour_distance_miles'], 33.75)
        self.assertEqual(result['total_driving_distance_miles'], 1313.30)

        # Stop 1 validations: 40.04 gal * $2.959/gal = $118.48
        stop1 = result['stops'][0]
        self.assertEqual(stop1['truckstop_id'], 61804)
        self.assertEqual(stop1['route_mile'], 382.89)
        self.assertEqual(stop1['retail_price'], 2.959)
        self.assertEqual(stop1['distance_from_route_miles'], 2.76)
        self.assertEqual(stop1['detour_to_station_miles'], 17.48)
        self.assertEqual(stop1['detour_from_station_miles'], 5.65)
        self.assertEqual(stop1['detour_distance_miles'], 23.13)
        self.assertEqual(stop1['actual_leg_distance_miles'], 400.37)
        self.assertLessEqual(stop1['actual_leg_distance_miles'], 500.0)
        self.assertEqual(stop1['fuel_consumed_on_leg_gallons'], 40.04)
        self.assertEqual(stop1['fuel_remaining_on_arrival_gallons'], 9.96)
        self.assertEqual(stop1['estimated_gallons_purchased'], 40.04)
        self.assertEqual(stop1['fuel_level_after_refueling_gallons'], 50.0)
        self.assertEqual(stop1['estimated_cost'], 118.48)

        # Stop 2 validations: 41.39 gal * $2.999/gal = $124.13
        stop2 = result['stops'][1]
        self.assertEqual(stop2['truckstop_id'], 71283)
        self.assertEqual(stop2['route_mile'], 787.80)
        self.assertEqual(stop2['retail_price'], 2.999)
        self.assertEqual(stop2['distance_from_route_miles'], 1.05)
        self.assertEqual(stop2['detour_to_station_miles'], 3.36)
        self.assertEqual(stop2['detour_from_station_miles'], 7.26)
        self.assertEqual(stop2['detour_distance_miles'], 10.62)
        # Leg 2 actual distance: 5.65 (return from stop 1) + 404.91 (highway) + 3.36 (to stop 2) = 413.92 mi
        self.assertEqual(stop2['actual_leg_distance_miles'], 413.92)
        self.assertLessEqual(stop2['actual_leg_distance_miles'], 500.0)
        self.assertEqual(stop2['fuel_consumed_on_leg_gallons'], 41.39)
        self.assertEqual(stop2['fuel_remaining_on_arrival_gallons'], 8.61)
        self.assertEqual(stop2['estimated_gallons_purchased'], 41.39)
        self.assertEqual(stop2['fuel_level_after_refueling_gallons'], 50.0)
        self.assertEqual(stop2['estimated_cost'], 124.13)

        # Final leg validations: 7.26 (return from stop 2) + 491.75 (highway to Miami) = 499.01 mi
        self.assertEqual(result['final_leg_main_route_distance_miles'], 491.75)
        self.assertEqual(result['final_leg_detour_distance_miles'], 7.26)
        self.assertEqual(result['final_leg_distance_miles'], 499.01)
        self.assertLessEqual(result['final_leg_distance_miles'], 500.0)
        self.assertEqual(result['final_leg_fuel_consumed_gallons'], 49.90)
        self.assertEqual(result['fuel_remaining_at_destination_gallons'], 0.10)

        # Aggregate totals & fuel conservation: $118.48 + $124.13 = $242.61
        self.assertEqual(result['total_gallons_purchased_at_stops'], 81.43)
        self.assertEqual(result['total_fuel_gallons'], 81.43)
        self.assertEqual(result['total_fuel_cost'], 242.61)
        self.assertEqual(result['estimated_trip_consumption_gallons'], 131.33)

        # Starting tank (50.0) + purchased (81.43) = 131.43 = consumed (131.33) + remaining (0.10)
        self.assertAlmostEqual(
            50.0 + result['total_gallons_purchased_at_stops'],
            result['estimated_trip_consumption_gallons'] + result['fuel_remaining_at_destination_gallons'],
            places=2
        )

    def test_route_requiring_no_stops_regression(self):
        """A route shorter than 500 miles requires no stops and reports zero detour distance."""
        candidate_stations = [
            {
                'truckstop_id': 9901,
                'truckstop_name': 'Interstate Gas',
                'retail_price': 3.15,
                'coordinates': [-75.0, 40.0],
                'route_mile': 150.0,
                'distance_from_route_miles': 2.0,
                'detour_distance_miles': 4.0,
            }
        ]

        result = self.optimizer.optimize(total_road_miles=350.0, candidate_stations=candidate_stations)

        self.assertTrue(result['feasible'])
        self.assertFalse(result['requires_refueling'])
        self.assertEqual(result['total_stops'], 0)
        self.assertEqual(result['main_route_distance_miles'], 350.0)
        self.assertEqual(result['total_detour_distance_miles'], 0.0)
        self.assertEqual(result['total_driving_distance_miles'], 350.0)
        self.assertEqual(result['total_fuel_cost'], 0.0)
        self.assertEqual(result['total_gallons_purchased_at_stops'], 0.0)
        self.assertEqual(result['estimated_trip_consumption_gallons'], 35.0)
        self.assertEqual(result['fuel_remaining_at_destination_gallons'], 15.0)
        self.assertEqual(len(result['stops']), 0)

    def test_station_detour_causes_leg_to_exceed_available_range(self):
        """
        When main-route mileage to a station is within 500 miles (e.g. 490 mi),
        but the station's actual detour (e.g. 15 mi) causes the leg (505 mi) to exceed the
        500-mile vehicle range, the optimizer detects range infeasibility.
        """
        candidate_stations = [
            {
                'truckstop_id': 9950,
                'truckstop_name': 'Far Off Route Stop',
                'retail_price': 3.00,
                'coordinates': [-78.0, 40.0],
                'route_mile': 490.0,  # 490 < 500 miles (would seem reachable on highway alone)
                'distance_from_route_miles': 12.0,
                'detour_to_station_miles': 15.0,  # 490 + 15 = 505.0 miles > 500.0 miles!
                'detour_from_station_miles': 15.0,
                'detour_distance_miles': 30.0,
            }
        ]

        result = self.optimizer.optimize(total_road_miles=800.0, candidate_stations=candidate_stations)

        self.assertFalse(result['feasible'])
        self.assertTrue(result['requires_refueling'])
        self.assertEqual(result['total_stops'], 0)
        self.assertIn("Infeasible route", result['message'])
        self.assertIn("500-mile", result['message'])

    def test_initial_fuel_with_station_detour_exceeds_available_fuel(self):
        """
        When departing with 15.0 gallons (150-mile range) and the station is at route mile 145.0,
        a 10-mile detour creates an actual leg of 155.0 miles, which exceeds available fuel.
        """
        candidate_stations = [
            {
                'truckstop_id': 9951,
                'truckstop_name': 'Detour Fuel Exceeded Stop',
                'retail_price': 3.10,
                'coordinates': [-75.0, 40.0],
                'route_mile': 145.0,  # 145 < 150 miles
                'distance_from_route_miles': 8.0,
                'detour_to_station_miles': 10.0,  # 145 + 10 = 155.0 miles > 150.0 miles!
                'detour_from_station_miles': 5.0,
                'detour_distance_miles': 15.0,
            }
        ]

        result = self.optimizer.optimize(
            total_road_miles=400.0,
            candidate_stations=candidate_stations,
            initial_fuel_gallons=15.0
        )

        self.assertFalse(result['feasible'])
        self.assertIn("Infeasible route", result['message'])

    def test_no_feasible_station_sequence_exists(self):
        """
        Regression test: When no candidate station sequence can bridge the route
        (e.g., all available stations leave gaps exceeding the vehicle range),
        the optimizer returns feasible=False with a clear explanatory message.
        """
        # Route is 1,200 miles long. First station is at mile 550 (> 500 miles).
        # Vehicle cannot even reach the first station from departure.
        candidate_stations = [
            {
                'truckstop_id': 9991,
                'truckstop_name': 'Too Far Station',
                'retail_price': 3.10,
                'coordinates': [-80.0, 35.0],
                'route_mile': 550.0,
                'distance_from_route_miles': 1.0,
                'detour_distance_miles': 2.0,
            },
            {
                'truckstop_id': 9992,
                'truckstop_name': 'Second Station',
                'retail_price': 3.15,
                'coordinates': [-82.0, 32.0],
                'route_mile': 900.0,
                'distance_from_route_miles': 1.0,
                'detour_distance_miles': 2.0,
            }
        ]

        result = self.optimizer.optimize(total_road_miles=1200.0, candidate_stations=candidate_stations)

        self.assertFalse(result['feasible'])
        self.assertTrue(result['requires_refueling'])
        self.assertEqual(result['total_stops'], 0)
        self.assertEqual(len(result['stops']), 0)
        self.assertIn("Infeasible route", result['message'])
        self.assertIn("exceeds the 500-mile", result['message'])

    def test_configurable_safety_reserve_enforces_buffer(self):
        """
        Tests that setting a configurable safety reserve (e.g. 25.0 miles / 2.5 gallons)
        restricts the usable range from 500.0 miles to 475.0 miles, preventing
        recommendations that arrive near zero fuel.
        """
        candidate_stations = [
            {
                'truckstop_id': 8881,
                'truckstop_name': 'Edge Station',
                'retail_price': 3.00,
                'coordinates': [-78.0, 40.0],
                'route_mile': 485.0,  # 485 <= 500, but > 475 (usable range with 25-mi reserve)
                'distance_from_route_miles': 0.5,
                'detour_to_station_miles': 1.0,
                'detour_from_station_miles': 1.0,
                'detour_distance_miles': 2.0,
            }
        ]

        # Without safety reserve: 485 + 1 = 486 <= 500 miles -> reachable
        res_no_reserve = self.optimizer.optimize(
            total_road_miles=900.0,
            candidate_stations=candidate_stations,
            safety_reserve_miles=0.0
        )
        self.assertEqual(res_no_reserve['usable_range_miles'], 500.0)

        # With 25-mile reserve: leg would be 486.0 miles > 475.0 miles -> marked infeasible
        res_with_reserve = self.optimizer.optimize(
            total_road_miles=900.0,
            candidate_stations=candidate_stations,
            safety_reserve_miles=25.0
        )
        self.assertEqual(res_with_reserve['safety_reserve_miles'], 25.0)
        self.assertEqual(res_with_reserve['usable_range_miles'], 475.0)
        self.assertFalse(res_with_reserve['feasible'])
        self.assertIn("Infeasible route", res_with_reserve['message'])

    def test_new_york_to_miami_with_safety_reserve_25_miles(self):
        """
        Regression test for New York, NY -> Miami, FL route (1,279.55 miles) with safety_reserve_miles: 25.0.
        Validates:
        1. Usable range is 475.0 miles (500.0 - 25.0).
        2. Three refueling stops are selected (CARTERS ONE STOP, QUIKTRIP #7135, Racetrac #2586).
        3. All 5 distance and detour identities hold exactly:
           - total_driving_distance_miles == main_route_distance_miles + total_detour_distance_miles (1341.07 == 1279.55 + 61.52)
           - detour_distance_miles == detour_to_station_miles + detour_from_station_miles for every stop
           - sum of actual driving legs + final leg == total_driving_distance_miles (400.37 + 413.92 + 378.92 + 147.86 == 1341.07)
           - final_leg_distance_miles == final_leg_main_route_distance_miles + final_leg_detour_distance_miles (147.86 == 136.58 + 11.28)
           - total_stops == len(stops) == 3
        4. Corridor progress sum: sum(distance_from_previous_leg_miles) + final_leg_main_route == 1279.55 miles.
        5. Every leg is strictly within the usable range (<= 475.0 miles).
        6. Fuel conservation balance: 50.0 initial + 119.32 purchased == 134.11 consumed + 35.21 destination remaining.
        7. Total refueling cost is $362.30 ($118.48 + $124.13 + $119.69).
        """
        total_road_miles = 1279.55
        candidate_stations = [
            {
                'truckstop_id': 61804,
                'truckstop_name': 'CARTERS ONE STOP',
                'address': 'I-95 & SR-301, EXIT 31 & SR-40',
                'city': 'Stony Creek',
                'state': 'VA',
                'retail_price': 2.959,
                'coordinates': [-77.444276, 36.936132],
                'projection_coordinates': [-77.394865, 36.930525],
                'route_mile': 382.89,
                'distance_from_route_miles': 2.76,
                'detour_to_station_miles': 17.48,
                'detour_from_station_miles': 5.65,
                'detour_distance_miles': 23.13,
            },
            {
                'truckstop_id': 71283,
                'truckstop_name': 'QUIKTRIP #7135',
                'address': 'US-17',
                'city': 'Hardeeville',
                'state': 'SC',
                'retail_price': 2.999,
                'coordinates': [-81.066865, 32.263512],
                'projection_coordinates': [-81.07978, 32.274072],
                'route_mile': 787.80,
                'distance_from_route_miles': 1.05,
                'detour_to_station_miles': 3.36,
                'detour_from_station_miles': 7.26,
                'detour_distance_miles': 10.62,
            },
            {
                'truckstop_id': 72893,
                'truckstop_name': 'Racetrac #2586',
                'address': 'I-95 & 20th ST',
                'city': 'Vero Beach',
                'state': 'FL',
                'retail_price': 3.159,
                'coordinates': [-80.403075, 27.632985],
                'projection_coordinates': [-80.502556, 27.592172],
                'route_mile': 1142.97,
                'distance_from_route_miles': 6.71,
                'detour_to_station_miles': 16.49,
                'detour_from_station_miles': 11.28,
                'detour_distance_miles': 27.77,
            }
        ]

        result = self.optimizer.optimize(
            total_road_miles=total_road_miles,
            candidate_stations=candidate_stations,
            safety_reserve_miles=25.0
        )

        self.assertTrue(result['feasible'])
        self.assertTrue(result['requires_refueling'])
        self.assertEqual(result['safety_reserve_miles'], 25.0)
        self.assertEqual(result['usable_range_miles'], 475.0)

        # Identity 5: Stop count equals length of returned stops array
        self.assertEqual(result['total_stops'], 3)
        self.assertEqual(len(result['stops']), 3)

        # Identity 1: total_driving_distance_miles = main_route_distance_miles + total_detour_distance_miles
        self.assertEqual(result['main_route_distance_miles'], 1279.55)
        self.assertEqual(result['total_detour_distance_miles'], 61.52)
        self.assertEqual(result['total_driving_distance_miles'], 1341.07)
        self.assertEqual(
            result['total_driving_distance_miles'],
            round(result['main_route_distance_miles'] + result['total_detour_distance_miles'], 2)
        )

        # Stop 1 validations
        stop1 = result['stops'][0]
        self.assertEqual(stop1['stop_number'], 1)
        self.assertEqual(stop1['truckstop_id'], 61804)
        self.assertEqual(stop1['retail_price'], 2.959)
        self.assertEqual(stop1['route_mile'], 382.89)
        self.assertEqual(stop1['distance_from_route_miles'], 2.76)
        self.assertEqual(stop1['distance_from_previous_leg_miles'], 382.89)
        self.assertEqual(stop1['detour_to_station_miles'], 17.48)
        self.assertEqual(stop1['detour_from_station_miles'], 5.65)
        self.assertEqual(stop1['detour_distance_miles'], 23.13)
        # Identity 2 on Stop 1
        self.assertEqual(stop1['detour_distance_miles'], round(stop1['detour_to_station_miles'] + stop1['detour_from_station_miles'], 2))
        self.assertEqual(stop1['actual_leg_distance_miles'], 400.37)
        self.assertLessEqual(stop1['actual_leg_distance_miles'], 475.0)
        self.assertEqual(stop1['fuel_consumed_on_leg_gallons'], 40.04)
        self.assertEqual(stop1['fuel_remaining_on_arrival_gallons'], 9.96)
        self.assertEqual(stop1['estimated_gallons_purchased'], 40.04)
        self.assertEqual(stop1['fuel_level_after_refueling_gallons'], 50.0)
        self.assertEqual(stop1['estimated_cost'], 118.48)

        # Stop 2 validations
        stop2 = result['stops'][1]
        self.assertEqual(stop2['stop_number'], 2)
        self.assertEqual(stop2['truckstop_id'], 71283)
        self.assertEqual(stop2['retail_price'], 2.999)
        self.assertEqual(stop2['route_mile'], 787.80)
        self.assertEqual(stop2['distance_from_route_miles'], 1.05)
        self.assertEqual(stop2['distance_from_previous_leg_miles'], 404.91)
        self.assertEqual(stop2['detour_to_station_miles'], 3.36)
        self.assertEqual(stop2['detour_from_station_miles'], 7.26)
        self.assertEqual(stop2['detour_distance_miles'], 10.62)
        # Identity 2 on Stop 2
        self.assertEqual(stop2['detour_distance_miles'], round(stop2['detour_to_station_miles'] + stop2['detour_from_station_miles'], 2))
        self.assertEqual(stop2['actual_leg_distance_miles'], 413.92)
        self.assertLessEqual(stop2['actual_leg_distance_miles'], 475.0)
        self.assertEqual(stop2['fuel_consumed_on_leg_gallons'], 41.39)
        self.assertEqual(stop2['fuel_remaining_on_arrival_gallons'], 8.61)
        self.assertEqual(stop2['estimated_gallons_purchased'], 41.39)
        self.assertEqual(stop2['fuel_level_after_refueling_gallons'], 50.0)
        self.assertEqual(stop2['estimated_cost'], 124.13)

        # Stop 3 validations
        stop3 = result['stops'][2]
        self.assertEqual(stop3['stop_number'], 3)
        self.assertEqual(stop3['truckstop_id'], 72893)
        self.assertEqual(stop3['retail_price'], 3.159)
        self.assertEqual(stop3['route_mile'], 1142.97)
        self.assertEqual(stop3['distance_from_route_miles'], 6.71)
        self.assertEqual(stop3['distance_from_previous_leg_miles'], 355.17)
        self.assertEqual(stop3['detour_to_station_miles'], 16.49)
        self.assertEqual(stop3['detour_from_station_miles'], 11.28)
        self.assertEqual(stop3['detour_distance_miles'], 27.77)
        # Identity 2 on Stop 3
        self.assertEqual(stop3['detour_distance_miles'], round(stop3['detour_to_station_miles'] + stop3['detour_from_station_miles'], 2))
        self.assertEqual(stop3['actual_leg_distance_miles'], 378.92)
        self.assertLessEqual(stop3['actual_leg_distance_miles'], 475.0)
        self.assertEqual(stop3['fuel_consumed_on_leg_gallons'], 37.89)
        self.assertEqual(stop3['fuel_remaining_on_arrival_gallons'], 12.11)
        self.assertEqual(stop3['estimated_gallons_purchased'], 37.89)
        self.assertEqual(stop3['fuel_level_after_refueling_gallons'], 50.0)
        self.assertEqual(stop3['estimated_cost'], 119.69)

        # Final leg validations & Identity 4
        self.assertEqual(result['final_leg_main_route_distance_miles'], 136.58)
        self.assertEqual(result['final_leg_detour_distance_miles'], 11.28)
        self.assertEqual(result['final_leg_distance_miles'], 147.86)
        self.assertEqual(
            result['final_leg_distance_miles'],
            round(result['final_leg_main_route_distance_miles'] + result['final_leg_detour_distance_miles'], 2)
        )
        self.assertLessEqual(result['final_leg_distance_miles'], 475.0)
        self.assertEqual(result['final_leg_fuel_consumed_gallons'], 14.79)
        self.assertEqual(result['fuel_remaining_at_destination_gallons'], 35.21)

        # Identity 3: Sum of all actual driving legs equals total_driving_distance_miles
        actual_legs_sum = round(
            stop1['actual_leg_distance_miles'] +
            stop2['actual_leg_distance_miles'] +
            stop3['actual_leg_distance_miles'] +
            result['final_leg_distance_miles'],
            2
        )
        self.assertEqual(actual_legs_sum, result['total_driving_distance_miles'])

        # Corridor highway progress sum equals main_route_distance_miles
        corridor_progress_sum = round(
            stop1['distance_from_previous_leg_miles'] +
            stop2['distance_from_previous_leg_miles'] +
            stop3['distance_from_previous_leg_miles'] +
            result['final_leg_main_route_distance_miles'],
            2
        )
        self.assertEqual(corridor_progress_sum, result['main_route_distance_miles'])

        # Fuel and cost aggregate validations
        self.assertEqual(result['total_gallons_purchased_at_stops'], 119.32)
        self.assertEqual(result['total_fuel_cost'], 362.30)
        self.assertEqual(result['estimated_trip_consumption_gallons'], 134.11)

        # Fuel conservation: starting 50.0 + purchased == consumed + remaining
        self.assertAlmostEqual(
            50.0 + result['total_gallons_purchased_at_stops'],
            result['estimated_trip_consumption_gallons'] + result['fuel_remaining_at_destination_gallons'],
            places=2
        )

    def test_safety_reserve_zero_vs_25_miles_comparison(self):
        """
        Validates the behavioral difference between safety_reserve_miles: 0.0 and 25.0:
        At reserve 0.0, the usable range is 500.0 miles and the final leg of 499.01 miles is allowed,
        resulting in 2 stops ($242.61).
        At reserve 25.0, the usable range is 475.0 miles, so the 499.01-mile leg is disallowed,
        requiring a 3rd stop at Vero Beach, FL to maintain the safety buffer ($362.30).
        """
        total_road_miles = 1279.55
        candidate_stations = [
            {
                'truckstop_id': 61804,
                'truckstop_name': 'CARTERS ONE STOP',
                'retail_price': 2.959,
                'coordinates': [-77.444276, 36.936132],
                'route_mile': 382.89,
                'detour_to_station_miles': 17.48,
                'detour_from_station_miles': 5.65,
                'detour_distance_miles': 23.13,
            },
            {
                'truckstop_id': 71283,
                'truckstop_name': 'QUIKTRIP #7135',
                'retail_price': 2.999,
                'coordinates': [-81.066865, 32.263512],
                'route_mile': 787.80,
                'detour_to_station_miles': 3.36,
                'detour_from_station_miles': 7.26,
                'detour_distance_miles': 10.62,
            },
            {
                'truckstop_id': 72893,
                'truckstop_name': 'Racetrac #2586',
                'retail_price': 3.159,
                'coordinates': [-80.403075, 27.632985],
                'route_mile': 1142.97,
                'detour_to_station_miles': 16.49,
                'detour_from_station_miles': 11.28,
                'detour_distance_miles': 27.77,
            }
        ]

        res_zero = self.optimizer.optimize(
            total_road_miles=total_road_miles,
            candidate_stations=candidate_stations,
            safety_reserve_miles=0.0
        )
        self.assertEqual(res_zero['total_stops'], 2)
        self.assertEqual(res_zero['usable_range_miles'], 500.0)
        self.assertEqual(res_zero['final_leg_distance_miles'], 499.01)
        self.assertLessEqual(res_zero['final_leg_distance_miles'], 500.0)

        res_25 = self.optimizer.optimize(
            total_road_miles=total_road_miles,
            candidate_stations=candidate_stations,
            safety_reserve_miles=25.0
        )
        self.assertEqual(res_25['total_stops'], 3)
        self.assertEqual(res_25['usable_range_miles'], 475.0)
        self.assertEqual(res_25['final_leg_distance_miles'], 147.86)
        self.assertLessEqual(res_25['final_leg_distance_miles'], 475.0)

    def test_calculation_consistency_invariants(self):
        """
        Systematic verification of the five core calculation identities across multi-stop routes:
        1. total_driving = main_route + total_detour
        2. detour_distance = detour_to + detour_from
        3. sum(actual_legs) + final_leg == total_driving
        4. final_leg = final_main + final_detour
        5. total_stops == len(stops)
        And fuel balance: initial + total_purchased == trip_consumed + destination_remaining.
        """
        candidates = [
            {
                'truckstop_id': 1,
                'route_mile': 300.0,
                'retail_price': 3.20,
                'coordinates': [-77.0, 39.0],
                'detour_to_station_miles': 5.0,
                'detour_from_station_miles': 7.0,
                'detour_distance_miles': 12.0,
            },
            {
                'truckstop_id': 2,
                'route_mile': 650.0,
                'retail_price': 3.10,
                'coordinates': [-80.0, 35.0],
                'detour_to_station_miles': 4.0,
                'detour_from_station_miles': 6.0,
                'detour_distance_miles': 10.0,
            }
        ]

        result = self.optimizer.optimize(
            total_road_miles=950.0,
            candidate_stations=candidates,
            safety_reserve_miles=25.0
        )

        self.assertTrue(result['feasible'])
        self.assertEqual(result['total_stops'], len(result['stops']))

        # Identity 1
        self.assertEqual(
            result['total_driving_distance_miles'],
            round(result['main_route_distance_miles'] + result['total_detour_distance_miles'], 2)
        )

        # Identity 2 on each stop
        for stop in result['stops']:
            self.assertEqual(
                stop['detour_distance_miles'],
                round(stop['detour_to_station_miles'] + stop['detour_from_station_miles'], 2)
            )

        # Identity 3: All legs sum to total driving distance
        legs_sum = round(sum(s['actual_leg_distance_miles'] for s in result['stops']) + result['final_leg_distance_miles'], 2)
        self.assertEqual(legs_sum, result['total_driving_distance_miles'])

        # Identity 4: Final leg equals main + detour
        self.assertEqual(
            result['final_leg_distance_miles'],
            round(result['final_leg_main_route_distance_miles'] + result['final_leg_detour_distance_miles'], 2)
        )

        # Fuel balance
        self.assertAlmostEqual(
            50.0 + result['total_gallons_purchased_at_stops'],
            result['estimated_trip_consumption_gallons'] + result['fuel_remaining_at_destination_gallons'],
            places=2
        )

    def test_optimizer_handles_boundary_and_invalid_safety_reserve(self):
        """Negative and excessive safety reserve values are cleanly handled and clamped by the optimizer."""
        candidates = [
            {
                'truckstop_id': 1,
                'route_mile': 300.0,
                'retail_price': 3.00,
                'coordinates': [-75.0, 40.0],
                'distance_from_route_miles': 1.0,
            }
        ]
        # Negative reserve is clamped to 0.0
        res_neg = self.optimizer.optimize(total_road_miles=600.0, candidate_stations=candidates, safety_reserve_miles=-10.0)
        self.assertEqual(res_neg['safety_reserve_miles'], 0.0)
        self.assertEqual(res_neg['usable_range_miles'], 500.0)

        # Excessive reserve (> max_range - 10) is clamped to 490.0
        res_excess = self.optimizer.optimize(total_road_miles=600.0, candidate_stations=candidates, safety_reserve_miles=600.0)
        self.assertEqual(res_excess['safety_reserve_miles'], 490.0)
        self.assertEqual(res_excess['usable_range_miles'], 10.0)




class RouteAPIFuelStopsTests(APITestCase):
    def setUp(self):
        # Create test fuel stations along a simulated route
        FuelStation.objects.create(
            truckstop_id=8801,
            truckstop_name="Pennsylvania Travel Stop",
            address="Exit 100",
            city="DuBois",
            state="PA",
            rack_id=1,
            retail_price=3.259,
            latitude=41.12,
            longitude=-78.76
        )
        FuelStation.objects.create(
            truckstop_id=8802,
            truckstop_name="Ohio Turnpike Fuel Stop",
            address="Exit 145",
            city="Amherst",
            state="OH",
            rack_id=2,
            retail_price=2.999,
            latitude=41.40,
            longitude=-82.22
        )

    def test_short_route_api_contract(self):
        def mock_requests(url, *args, **kwargs):
            class MockResp:
                status_code = 200
                def json(self):
                    if 'nominatim' in url or 'search' in url:
                        q = kwargs.get('params', {}).get('q', '')
                        lat = '40.71' if 'new york' in q.lower() else '39.95'
                        lon = '-74.00' if 'new york' in q.lower() else '-75.16'
                        return [{'display_name': q, 'lat': lat, 'lon': lon, 'address': {'country_code': 'us'}}]
                    elif 'route' in url:
                        return {
                            'code': 'Ok',
                            'routes': [{
                                'distance': 152000.0,  # ~94.4 miles
                                'duration': 7200.0,
                                'geometry': {
                                    'type': 'LineString',
                                    'coordinates': [[-74.00, 40.71], [-75.16, 39.95]]
                                }
                            }]
                        }
                    return {}

            return MockResp()

        with patch('requests.get', side_effect=mock_requests):
            response = self.client.post('/api/route/', {'start': 'New York, NY', 'destination': 'Philadelphia, PA'}, format='json')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            data = response.json()

            # Verify existing API contract
            self.assertIn('start', data)
            self.assertIn('destination', data)
            self.assertIn('distance', data)
            self.assertIn('duration_seconds', data)
            self.assertIn('geometry', data)
            self.assertIn('metadata', data)

            # Verify fuel_stops contract
            self.assertIn('fuel_stops', data)
            fuel = data['fuel_stops']
            self.assertTrue(fuel['feasible'])
            self.assertFalse(fuel['requires_refueling'])
            self.assertEqual(fuel['total_stops'], 0)
            self.assertEqual(fuel['total_fuel_cost'], 0.0)
            self.assertEqual(fuel['total_gallons_purchased_at_stops'], 0.0)
            self.assertGreater(fuel['estimated_trip_consumption_gallons'], 0.0)
            self.assertGreater(fuel['fuel_remaining_at_destination_gallons'], 0.0)

    def test_long_route_api_contract(self):
        # Mock route of 800 miles from New York to Chicago passing through Amherst, OH
        def mock_requests(url, *args, **kwargs):
            class MockResp:
                status_code = 200
                def json(self):
                    if 'nominatim' in url or 'search' in url:
                        q = kwargs.get('params', {}).get('q', '')
                        lat = '40.71' if 'new york' in q.lower() else '41.87'
                        lon = '-74.00' if 'new york' in q.lower() else '-87.62'
                        return [{'display_name': q, 'lat': lat, 'lon': lon, 'address': {'country_code': 'us'}}]
                    elif 'route' in url:
                        return {
                            'code': 'Ok',
                            'routes': [{
                                'distance': 1287475.2,  # exactly 800.0 miles
                                'duration': 43200.0,
                                'geometry': {
                                    'type': 'LineString',
                                    'coordinates': [
                                        [-74.00, 40.71],   # Start (mile 0)
                                        [-78.76, 41.12],   # DuBois PA (~mile 250)
                                        [-82.22, 41.40],   # Amherst OH (~mile 450)
                                        [-87.62, 41.87]    # Chicago IL (mile 800)
                                    ]
                                }
                            }]
                        }
                    return {}

            return MockResp()

        with patch('requests.get', side_effect=mock_requests):
            response = self.client.post('/api/route/', {'start': 'New York, NY', 'destination': 'Chicago, IL'}, format='json')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            data = response.json()

            self.assertIn('fuel_stops', data)
            fuel = data['fuel_stops']
            self.assertTrue(fuel['feasible'])
            self.assertTrue(fuel['requires_refueling'])
            self.assertGreaterEqual(fuel['total_stops'], 1)
            self.assertGreater(fuel['total_fuel_cost'], 0.0)
            self.assertEqual(fuel['estimated_trip_consumption_gallons'], 80.0)

            # Check stop fields
            first_stop = fuel['stops'][0]
            self.assertIn('stop_number', first_stop)
            self.assertIn('truckstop_name', first_stop)
            self.assertIn('retail_price', first_stop)
            self.assertIn('coordinates', first_stop)
            self.assertIn('route_mile', first_stop)
            self.assertIn('distance_from_previous_leg_miles', first_stop)
            self.assertIn('fuel_consumed_on_leg_gallons', first_stop)
            self.assertIn('fuel_remaining_on_arrival_gallons', first_stop)
            self.assertIn('estimated_gallons_purchased', first_stop)
            self.assertIn('fuel_level_after_refueling_gallons', first_stop)
            self.assertIn('estimated_cost', first_stop)

            # Validate that purchase never exceeds 50.0 gallons
            self.assertLessEqual(first_stop['estimated_gallons_purchased'], 50.0)

            # Validate stop cost equals purchased * retail_price
            expected_cost = round(first_stop['estimated_gallons_purchased'] * first_stop['retail_price'], 2)
            self.assertEqual(first_stop['estimated_cost'], expected_cost)

            # Metadata vehicle specs
            specs = data['metadata']['vehicle_specs']
            self.assertEqual(specs['tank_capacity_gallons'], 50.0)
            self.assertEqual(specs['estimated_trip_consumption_gallons'], 80.0)

    def test_custom_proximity_and_initial_fuel_parameters(self):
        def mock_requests(url, *args, **kwargs):
            class MockResp:
                status_code = 200
                def json(self):
                    if 'nominatim' in url or 'search' in url:
                        return [{'display_name': 'Loc', 'lat': '40.0', 'lon': '-74.0', 'address': {'country_code': 'us'}}]
                    elif 'route' in url:
                        return {
                            'code': 'Ok',
                            'routes': [{
                                'distance': 800000.0,  # ~497 miles
                                'duration': 28000.0,
                                'geometry': {'type': 'LineString', 'coordinates': [[-74.0, 40.0], [-78.0, 40.0]]}
                            }]
                        }
                    return {}
            return MockResp()

        with patch('requests.get', side_effect=mock_requests):
            # Normally 497 miles <= 500 miles (no refuel if full tank).
            # But with initial_fuel_gallons = 10.0 (100 miles range), it REQUIRES refueling!
            payload = {
                'start': 'Start City, ST',
                'destination': 'End City, ST',
                'max_proximity_miles': 25.0,
                'initial_fuel_gallons': 10.0
            }
            response = self.client.post('/api/route/', payload, format='json')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            data = response.json()
            self.assertTrue(data['fuel_stops']['requires_refueling'])
            self.assertEqual(data['metadata']['vehicle_specs']['proximity_threshold_miles'], 25.0)
            self.assertEqual(data['metadata']['vehicle_specs']['initial_fuel_gallons'], 10.0)

    def test_route_api_multi_stop_with_detours_distinguished(self):
        """
        Integration test verifying that POST /api/route/ returns compatible fields
        while clearly distinguishing main-route mileage, detour mileage, and actual driving mileage.
        """
        def mock_requests(url, *args, **kwargs):
            class MockResp:
                status_code = 200
                def json(self):
                    if 'nominatim' in url or 'search' in url:
                        q = kwargs.get('params', {}).get('q', '')
                        lat = '40.71' if 'new york' in q.lower() else '25.76'
                        lon = '-74.00' if 'new york' in q.lower() else '-80.19'
                        return [{'display_name': q, 'lat': lat, 'lon': lon, 'address': {'country_code': 'us'}}]
                    elif 'route' in url:
                        # Main route or detour query
                        return {
                            'code': 'Ok',
                            'routes': [{
                                'distance': 1287475.2,  # 800.0 miles
                                'duration': 43200.0,
                                'geometry': {
                                    'type': 'LineString',
                                    'coordinates': [
                                        [-74.00, 40.71],
                                        [-78.76, 41.12],
                                        [-82.22, 41.40],
                                        [-87.62, 41.87]
                                    ]
                                }
                            }]
                        }
                    return {}
            return MockResp()

        with patch('requests.get', side_effect=mock_requests):
            response = self.client.post('/api/route/', {'start': 'New York, NY', 'destination': 'Miami, FL'}, format='json')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            data = response.json()

            # Compatible distance root block
            dist = data['distance']
            self.assertIn('miles', dist)
            self.assertIn('main_route_miles', dist)
            self.assertIn('total_detour_miles', dist)
            self.assertIn('total_driving_miles', dist)
            self.assertEqual(dist['total_driving_miles'], round(dist['main_route_miles'] + dist['total_detour_miles'], 2))

            # Fuel stops block
            fuel = data['fuel_stops']
            self.assertIn('main_route_distance_miles', fuel)
            self.assertIn('total_detour_distance_miles', fuel)
            self.assertIn('total_driving_distance_miles', fuel)
            self.assertIn('total_gallons_purchased_at_stops', fuel)
            self.assertIn('estimated_trip_consumption_gallons', fuel)
            self.assertIn('fuel_remaining_at_destination_gallons', fuel)

            if fuel['stops']:
                st0 = fuel['stops'][0]
                self.assertIn('detour_to_station_miles', st0)
                self.assertIn('detour_from_station_miles', st0)
                self.assertIn('detour_distance_miles', st0)
                self.assertIn('actual_leg_distance_miles', st0)
                self.assertLessEqual(st0['actual_leg_distance_miles'], 500.0)

