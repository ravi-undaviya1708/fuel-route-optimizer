import logging
from django.db import connection, models
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from fuel_optimizer.models import FuelStation
from fuel_optimizer.serializers import RouteRequestSerializer, FuelStationSerializer
from fuel_optimizer.services.geocoding import NominatimGeocodingService
from fuel_optimizer.services.routing import OSRMRouteService
from fuel_optimizer.services.fuel_optimizer import (
    RouteFuelOptimizer,
    compute_calibrated_polyline_distances,
    find_stations_along_corridor,
)

logger = logging.getLogger(__name__)


class RouteView(APIView):
    """
    POST /api/route/
    Calculates driving route between start and destination addresses within the USA.
    Returns road distance (miles & km), duration, GeoJSON geometry, and optimal fuel stops.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.geocoding_service = NominatimGeocodingService()
        self.routing_service = OSRMRouteService()
        self.fuel_optimizer = RouteFuelOptimizer(max_range_miles=500.0, mpg=10.0)

    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        start_query = serializer.validated_data['start']
        dest_query = serializer.validated_data['destination']
        max_proximity = serializer.validated_data.get('max_proximity_miles', 15.0)
        initial_fuel = serializer.validated_data.get('initial_fuel_gallons', 50.0)
        safety_reserve = serializer.validated_data.get('safety_reserve_miles', 0.0)

        logger.info("Calculating route from '%s' to '%s' (proximity: %.1f mi, initial_fuel: %.1f gal, reserve: %.1f mi)",
                    start_query, dest_query, max_proximity, initial_fuel, safety_reserve)

        # 1. Geocode start location
        start_geo = self.geocoding_service.geocode(start_query)

        # 2. Geocode destination location
        dest_geo = self.geocoding_service.geocode(dest_query)

        # 3. Request OSRM driving route between coordinates [lon, lat]
        start_coords = (start_geo['longitude'], start_geo['latitude'])
        dest_coords = (dest_geo['longitude'], dest_geo['latitude'])

        route_info = self.routing_service.get_route(start_coords, dest_coords)

        # 4. Route geometry and corridor fuel stop optimization
        polyline_coords = route_info['geometry']['coordinates']
        total_miles = route_info['distance_miles']

        cum_distances = compute_calibrated_polyline_distances(polyline_coords, total_miles)
        corridor_stations = find_stations_along_corridor(
            polyline_coords, cum_distances, max_proximity_miles=max_proximity
        )

        fuel_plan = self.fuel_optimizer.optimize(
            total_road_miles=total_miles,
            candidate_stations=corridor_stations,
            initial_fuel_gallons=initial_fuel,
            routing_service=self.routing_service,
            safety_reserve_miles=safety_reserve,
        )

        response_data = {
            'start': {
                'query': start_query,
                'name': start_geo['display_name'],
                'coordinates': start_geo['coordinates'],  # [lon, lat]
            },
            'destination': {
                'query': dest_query,
                'name': dest_geo['display_name'],
                'coordinates': dest_geo['coordinates'],  # [lon, lat]
            },
            'distance': {
                'miles': route_info['distance_miles'],
                'kilometers': route_info['distance_kilometers'],
                'meters': route_info['distance_meters'],
                'main_route_miles': fuel_plan.get('main_route_distance_miles', route_info['distance_miles']),
                'total_detour_miles': fuel_plan.get('total_detour_distance_miles', 0.0),
                'total_driving_miles': fuel_plan.get('total_driving_distance_miles', route_info['distance_miles']),
            },
            'duration_seconds': route_info['duration_seconds'],
            'duration_formatted': route_info['duration_formatted'],
            'geometry': route_info['geometry'],
            'fuel_stops': fuel_plan,
            'metadata': {
                'routing_provider': route_info['provider'],
                'geocoding_provider': 'OpenStreetMap Nominatim',
                'cached': {
                    'start_geocoding': start_geo.get('cached', False),
                    'destination_geocoding': dest_geo.get('cached', False),
                    'route': route_info.get('cached', False),
                },
                'vehicle_specs': {
                    'max_range_miles': 500,
                    'mpg': 10,
                    'tank_capacity_gallons': 50.0,
                    'initial_fuel_gallons': initial_fuel,
                    'safety_reserve_miles': fuel_plan.get('safety_reserve_miles', safety_reserve),
                    'usable_range_miles': fuel_plan.get('usable_range_miles', 500.0 - safety_reserve),
                    'main_route_distance_miles': fuel_plan.get('main_route_distance_miles', total_miles),
                    'total_detour_distance_miles': fuel_plan.get('total_detour_distance_miles', 0.0),
                    'total_driving_distance_miles': fuel_plan.get('total_driving_distance_miles', total_miles),
                    'estimated_trip_consumption_gallons': fuel_plan.get('estimated_trip_consumption_gallons', round(total_miles / 10.0, 2)),
                    'estimated_fuel_gallons': fuel_plan.get('estimated_trip_consumption_gallons', round(total_miles / 10.0, 2)),
                    'requires_refueling': fuel_plan['requires_refueling'],
                    'feasible': fuel_plan['feasible'],
                    'candidate_stations_in_corridor': len(corridor_stations),
                    'proximity_threshold_miles': max_proximity,
                },
            },
        }

        return Response(response_data, status=status.HTTP_200_OK)


class HealthCheckView(APIView):
    """
    GET /api/health/
    Lightweight health check returning system readiness and database status.
    """

    def get(self, request):
        db_ok = True
        error_msg = None
        total_stations = 0
        stations_with_coords = 0

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            total_stations = FuelStation.objects.count()
            stations_with_coords = FuelStation.objects.filter(latitude__isnull=False).count()
        except Exception as exc:
            db_ok = False
            error_msg = str(exc)
            logger.error("Health check DB failure: %s", exc)

        response_payload = {
            'status': 'healthy' if db_ok else 'unhealthy',
            'service': 'fuel-route-optimizer',
            'database': {
                'connected': db_ok,
                'error': error_msg,
                'total_fuel_stations': total_stations,
                'stations_with_coordinates': stations_with_coords,
            },
            'version': '1.0.0 (Day 1)',
        }

        http_status = status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(response_payload, status=http_status)


class FuelStationPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


class FuelStationListView(ListAPIView):
    """
    GET /api/fuel-stations/
    Paginated read-only list of fuel stations with filtering by state and search.
    Query params:
        - state: Filter by 2-letter state code (e.g. ?state=TX)
        - search: Search in name, city, or address
        - ordering: Order by field (e.g. ?ordering=retail_price or ?ordering=-retail_price)
    """
    serializer_class = FuelStationSerializer
    pagination_class = FuelStationPagination

    def get_queryset(self):
        queryset = FuelStation.objects.all()

        state = self.request.query_params.get('state')
        if state:
            queryset = queryset.filter(state__iexact=state.strip())

        search = self.request.query_params.get('search')
        if search:
            s = search.strip()
            queryset = queryset.filter(
                models.Q(truckstop_name__icontains=s) |
                models.Q(city__icontains=s) |
                models.Q(address__icontains=s)
            )

        ordering = self.request.query_params.get('ordering')
        if ordering:
            allowed_orderings = ['retail_price', '-retail_price', 'truckstop_name', '-truckstop_name', 'state', '-state']
            if ordering in allowed_orderings:
                queryset = queryset.order_by(ordering)

        return queryset
