import math
import logging
from typing import List, Dict, Any, Tuple, Optional
from fuel_optimizer.models import FuelStation

logger = logging.getLogger(__name__)

EARTH_RADIUS_MILES = 3958.8


def haversine_distance_miles(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Calculates great-circle distance between two points in miles using Haversine formula."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2.0) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2.0) ** 2)
    return 2.0 * EARTH_RADIUS_MILES * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def compute_calibrated_polyline_distances(
    coordinates: List[List[float]],
    total_road_miles: float
) -> List[float]:
    """
    Computes calibrated cumulative road distance (in miles) along the route polyline vertices.
    Coordinates are in GeoJSON [lon, lat] format.
    """
    if not coordinates:
        return []
    if len(coordinates) == 1:
        return [0.0]

    cum_dist = [0.0]
    for i in range(1, len(coordinates)):
        d = haversine_distance_miles(
            coordinates[i - 1][0], coordinates[i - 1][1],
            coordinates[i][0], coordinates[i][1]
        )
        cum_dist.append(cum_dist[-1] + d)

    raw_total = cum_dist[-1]
    if raw_total > 0 and total_road_miles > 0:
        scale_factor = total_road_miles / raw_total
        return [round(d * scale_factor, 4) for d in cum_dist]
    return cum_dist


def find_stations_along_corridor(
    coordinates: List[List[float]],
    cum_distances: List[float],
    max_proximity_miles: float = 15.0
) -> List[Dict[str, Any]]:
    """
    Identifies and projects fuel stations along the road corridor within max_proximity_miles.
    Uses bounding box pre-filtering followed by segment-level perpendicular projection.
    Returns sorted list of candidate station dicts with route_mile and distance_from_route.
    """
    if not coordinates or len(coordinates) < 2:
        return []

    lons = [c[0] for c in coordinates]
    lats = [c[1] for c in coordinates]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    # 1 degree lat is ~69 miles; add buffer
    lat_buffer = (max_proximity_miles / 69.0) + 0.1
    mid_lat = (min_lat + max_lat) / 2.0
    lon_scale = math.cos(math.radians(mid_lat))
    lon_buffer = (max_proximity_miles / (69.0 * max(0.1, lon_scale))) + 0.1

    candidate_stations = list(FuelStation.objects.filter(
        latitude__range=(min_lat - lat_buffer, max_lat + lat_buffer),
        longitude__range=(min_lon - lon_buffer, max_lon + lon_buffer),
        latitude__isnull=False,
        longitude__isnull=False
    ).only('id', 'truckstop_id', 'truckstop_name', 'address', 'city', 'state', 'retail_price', 'latitude', 'longitude'))

    if not candidate_stations:
        return []

    # Polyline vertex stride indexing for fast spatial search
    num_coords = len(coordinates)
    stride = max(1, num_coords // 400)
    sample_indices = list(range(0, num_coords, stride))
    if sample_indices[-1] != num_coords - 1:
        sample_indices.append(num_coords - 1)

    matched = []
    for s in candidate_stations:
        slat = s.latitude
        slon = s.longitude

        # Find closest sampled polyline index
        best_sample_idx = min(
            sample_indices,
            key=lambda idx: (coordinates[idx][0] - slon) ** 2 + (coordinates[idx][1] - slat) ** 2
        )

        # Inspect local window around the closest sample vertex
        window_start = max(0, best_sample_idx - stride * 2)
        window_end = min(num_coords - 1, best_sample_idx + stride * 2)

        min_perp_dist = float('inf')
        best_mile = 0.0
        best_proj_lon = 0.0
        best_proj_lat = 0.0

        for j in range(window_start, window_end):
            p1 = coordinates[j]
            p2 = coordinates[j + 1]

            # Flat-earth projection onto segment [p1, p2]
            dx = (p2[0] - p1[0]) * math.cos(math.radians((p1[1] + p2[1]) / 2.0))
            dy = p2[1] - p1[1]
            px = (slon - p1[0]) * math.cos(math.radians((p1[1] + p2[1]) / 2.0))
            py = slat - p1[1]

            len_sq = dx * dx + dy * dy
            t = 0.0 if len_sq == 0 else max(0.0, min(1.0, (px * dx + py * dy) / len_sq))

            proj_lon = p1[0] + t * (p2[0] - p1[0])
            proj_lat = p1[1] + t * (p2[1] - p1[1])

            dist_to_seg = haversine_distance_miles(slon, slat, proj_lon, proj_lat)
            if dist_to_seg < min_perp_dist:
                min_perp_dist = dist_to_seg
                seg_d1 = cum_distances[j]
                seg_d2 = cum_distances[j + 1]
                best_mile = seg_d1 + t * (seg_d2 - seg_d1)
                best_proj_lon = proj_lon
                best_proj_lat = proj_lat

        if min_perp_dist <= max_proximity_miles:
            matched.append({
                'station': s,
                'truckstop_id': s.truckstop_id,
                'truckstop_name': s.truckstop_name,
                'address': s.address,
                'city': s.city,
                'state': s.state,
                'retail_price': float(s.retail_price),
                'coordinates': [s.longitude, s.latitude],
                'projection_coordinates': [round(best_proj_lon, 6), round(best_proj_lat, 6)],
                'route_mile': round(best_mile, 2),
                'distance_from_route_miles': round(min_perp_dist, 2),
            })

    # Sort candidates by route mile
    matched.sort(key=lambda x: x['route_mile'])

    # Deduplicate stations located within 0.25 miles of each other, keeping lowest price
    deduped = []
    for item in matched:
        if deduped and abs(item['route_mile'] - deduped[-1]['route_mile']) < 0.25:
            if item['retail_price'] < deduped[-1]['retail_price']:
                deduped[-1] = item
        else:
            deduped.append(item)

    return deduped


class RouteFuelOptimizer:
    """
    Optimizes fuel stop selection along a driving route.
    Assumptions:
    - Vehicle starts with a full tank of fuel (50.0 gallons = 500.0 miles maximum range at 10.0 MPG).
    - Maximum single travel leg without refueling cannot exceed 500.0 miles.
    - At each recommended stop, vehicle refills to full capacity.
    - Selects cost-effective stations along the corridor while accounting for station detours.
    """

    def __init__(
        self,
        max_range_miles: float = 500.0,
        mpg: float = 10.0,
        safety_reserve_miles: float = 0.0,
        routing_service: Optional[Any] = None
    ):
        self.max_range = max_range_miles
        self.mpg = mpg
        self.safety_reserve = float(safety_reserve_miles)
        self.usable_range = max_range_miles - self.safety_reserve
        self.routing_service = routing_service

    def get_station_detour(
        self,
        station: Dict[str, Any],
        routing_service: Optional[Any] = None,
        cache: Optional[Dict[Any, Tuple[float, float, float]]] = None
    ) -> Tuple[float, float, float]:
        """
        Determines driving distance to station from route and from station back to route.
        Returns: (detour_to_station_miles, detour_from_station_miles, total_detour_miles).

        If pre-computed in station dict (e.g. detour_to_station_miles, detour_from_station_miles,
        or detour_distance_miles), returns those values.
        Otherwise, if routing_service is available and projection coordinates exist,
        queries the routing service to obtain actual driving road distances.
        If routing is not feasible or service is unavailable, returns (0.0, 0.0, 0.0).
        """
        sid = station.get('truckstop_id') or station.get('id')
        if cache is not None and sid in cache:
            return cache[sid]

        d_to = None
        d_from = None

        if 'detour_to_station_miles' in station and 'detour_from_station_miles' in station:
            d_to = float(station['detour_to_station_miles'])
            d_from = float(station['detour_from_station_miles'])
        elif 'detour_distance_miles' in station:
            tot = float(station['detour_distance_miles'])
            d_to = round(tot / 2.0, 2)
            d_from = round(tot - d_to, 2)
        else:
            service = routing_service or self.routing_service
            p_coords = station.get('projection_coordinates')
            s_coords = station.get('coordinates')
            if service and p_coords and s_coords and len(p_coords) >= 2 and len(s_coords) >= 2:
                # If projection and station coordinates are practically identical, detour is zero
                if abs(p_coords[0] - s_coords[0]) < 1e-4 and abs(p_coords[1] - s_coords[1]) < 1e-4:
                    d_to = 0.0
                    d_from = 0.0
                else:
                    try:
                        to_res = service.get_route((p_coords[0], p_coords[1]), (s_coords[0], s_coords[1]))
                        from_res = service.get_route((s_coords[0], s_coords[1]), (p_coords[0], p_coords[1]))
                        d_to = round(float(to_res['distance_miles']), 2)
                        d_from = round(float(from_res['distance_miles']), 2)
                    except Exception as exc:
                        logger.debug("Routing detour lookup failed for station %s: %s", sid, exc)
                        # Fallback to perpendicular offset if routing service errored
                        perp = float(station.get('distance_from_route_miles', 0.0))
                        d_to = round(perp, 2)
                        d_from = round(perp, 2)
            elif not service:
                d_to = 0.0
                d_from = 0.0

        if d_to is None or d_from is None:
            perp = float(station.get('distance_from_route_miles', 0.0))
            d_to = round(perp, 2)
            d_from = round(perp, 2)

        tot_d = round(d_to + d_from, 2)
        res = (round(d_to, 2), round(d_from, 2), tot_d)
        if cache is not None and sid:
            cache[sid] = res
        return res

    def optimize(
        self,
        total_road_miles: float,
        candidate_stations: List[Dict[str, Any]],
        initial_fuel_gallons: Optional[float] = None,
        routing_service: Optional[Any] = None,
        safety_reserve_miles: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Executes fuel stop optimization for the route with actual driving detour accounting.

        Fuel Accounting Rules:
        - Tank capacity is 50.0 gallons (500.0 miles at 10.0 MPG).
        - Vehicle starts with initial_fuel_gallons (defaults to full tank: 50.0 gallons).
        - Driving leg distance = main_route_segment + detours to/from fuel stations.
        - Fuel consumed over a segment = actual_driving_leg_miles / 10.0 MPG.
        - Fuel remaining on arrival = previous_fuel - segment_consumption.
        - At each stop, vehicle refills to full: gallons purchased = tank_capacity - fuel_remaining.
        - Gallons purchased can never exceed tank capacity (50.0 gallons).
        - Cost at each stop = gallons purchased at that stop * retail_price.
        - total_fuel_cost is the total stop-refueling expenditure (excluding initial tank fuel).
        - estimated_trip_consumption_gallons is total actual driving distance / 10.0 MPG.
        - Configurable safety reserve ensures vehicle never arrives on fumes.
        """
        reserve_miles = float(safety_reserve_miles) if safety_reserve_miles is not None else self.safety_reserve
        reserve_miles = max(0.0, min(self.max_range - 10.0, reserve_miles))
        usable_range = round(self.max_range - reserve_miles, 2)

        tank_capacity_gallons = round(self.max_range / self.mpg, 2)
        start_fuel_gallons = float(initial_fuel_gallons) if initial_fuel_gallons is not None else tank_capacity_gallons
        start_fuel_gallons = max(0.0, min(tank_capacity_gallons, round(start_fuel_gallons, 2)))
        initial_range = round(start_fuel_gallons * self.mpg, 2)
        initial_usable_range = round(max(0.0, initial_range - reserve_miles), 2)

        # Sanitize and validate candidate stations
        valid_candidates = []
        for s in candidate_stations:
            price = s.get('retail_price')
            mile = s.get('route_mile')
            coords = s.get('coordinates')
            if price is None or not isinstance(price, (int, float)) or price <= 0:
                continue
            if mile is None or not isinstance(mile, (int, float)) or mile < 0:
                continue
            if not coords or not isinstance(coords, (list, tuple)) or len(coords) < 2:
                continue
            if coords[0] is None or coords[1] is None:
                continue
            valid_candidates.append(s)

        valid_candidates.sort(key=lambda x: x['route_mile'])

        # Case 1: Route reachable on initial fuel without stops
        if total_road_miles <= initial_usable_range:
            gallons_used = round(total_road_miles / self.mpg, 2)
            fuel_remaining = round(start_fuel_gallons - gallons_used, 2)
            return {
                'feasible': True,
                'requires_refueling': False,
                'initial_fuel_assumption': (
                    f"Vehicle departs with {start_fuel_gallons:.1f} gallons ({initial_range:.1f} miles range). "
                    f"Initial tank fuel is pre-existing; no refueling stops required."
                ),
                'safety_reserve_miles': reserve_miles,
                'usable_range_miles': usable_range,
                'main_route_distance_miles': round(total_road_miles, 2),
                'total_detour_distance_miles': 0.0,
                'total_driving_distance_miles': round(total_road_miles, 2),
                'total_stops': 0,
                'total_fuel_gallons': 0.0,
                'total_gallons_purchased_at_stops': 0.0,
                'estimated_trip_consumption_gallons': gallons_used,
                'fuel_remaining_at_destination_gallons': fuel_remaining,
                'total_fuel_cost': 0.0,
                'final_leg_main_route_distance_miles': round(total_road_miles, 2),
                'final_leg_detour_distance_miles': 0.0,
                'final_leg_distance_miles': round(total_road_miles, 2),
                'final_leg_fuel_consumed_gallons': gallons_used,
                'stops': [],
                'algorithm': (
                    "Greedy Lookahead Corridor Search — verified route distance is within starting fuel range."
                ),
                'message': f"Route distance ({total_road_miles:.1f} miles) is within vehicle range. No refueling needed."
            }

        # Case 2: Route requiring refueling stops
        current_pos = 0.0
        current_fuel = start_fuel_gallons
        last_detour_from = 0.0
        selected_stops: List[Dict[str, Any]] = []
        stop_index = 1
        detour_cache: Dict[Any, Tuple[float, float, float]] = {}

        while True:
            # Check if destination is reachable directly from current position and fuel state
            final_driving_leg = round(last_detour_from + (total_road_miles - current_pos), 2)
            available_range = round(max(0.0, (current_fuel * self.mpg) - reserve_miles), 2)
            if final_driving_leg <= available_range and final_driving_leg <= usable_range:
                # Destination reachable directly
                break

            # Find candidates ahead of current position
            candidates_ahead = [s for s in valid_candidates if s['route_mile'] > current_pos]
            if not candidates_ahead:
                break

            # Preliminary candidate filtering using estimated detours to minimize external queries
            preliminary_pool = []
            for s in candidates_ahead:
                main_dist = s['route_mile'] - current_pos
                if main_dist > (current_fuel * self.mpg):
                    # Candidates are sorted by route mile; subsequent stations are even further
                    break

                if 'detour_to_station_miles' in s and 'detour_from_station_miles' in s:
                    est_d_to = float(s['detour_to_station_miles'])
                    est_d_from = float(s['detour_from_station_miles'])
                elif 'detour_distance_miles' in s:
                    est_tot = float(s['detour_distance_miles'])
                    est_d_to = round(est_tot / 2.0, 2)
                    est_d_from = round(est_tot - est_d_to, 2)
                elif routing_service or self.routing_service:
                    est_perp = float(s.get('distance_from_route_miles', 0.0))
                    est_d_to = est_perp
                    est_d_from = est_perp
                else:
                    est_d_to = 0.0
                    est_d_from = 0.0

                est_leg = round(last_detour_from + main_dist + est_d_to, 2)
                if est_leg <= available_range and est_leg <= usable_range:
                    preliminary_pool.append({
                        'station': s,
                        'est_d_to': est_d_to,
                        'est_d_from': est_d_from,
                        'est_d_tot': round(est_d_to + est_d_from, 2),
                        'est_leg': est_leg
                    })

            if not preliminary_pool:
                # Infeasible gap: no stations reachable within available fuel and range
                gap_start = round(current_pos, 1)
                gap_limit = round(current_pos + available_range, 1)
                next_station = next((s for s in valid_candidates if s['route_mile'] > current_pos), None)
                next_mile = round(next_station['route_mile'], 1) if next_station else round(total_road_miles, 1)

                purchased_so_far = round(sum(s['estimated_gallons_purchased'] for s in selected_stops), 2)
                cost_so_far = round(sum(s['estimated_cost'] for s in selected_stops), 2)
                detour_so_far = round(sum(s['detour_distance_miles'] for s in selected_stops), 2)

                return {
                    'feasible': False,
                    'requires_refueling': True,
                    'initial_fuel_assumption': (
                        f"Vehicle departs with {start_fuel_gallons:.1f} gallons ({initial_range:.1f} miles range)."
                    ),
                    'safety_reserve_miles': reserve_miles,
                    'usable_range_miles': usable_range,
                    'main_route_distance_miles': round(total_road_miles, 2),
                    'total_detour_distance_miles': detour_so_far,
                    'total_driving_distance_miles': round(total_road_miles + detour_so_far, 2),
                    'total_stops': len(selected_stops),
                    'total_fuel_gallons': purchased_so_far,
                    'total_gallons_purchased_at_stops': purchased_so_far,
                    'estimated_trip_consumption_gallons': round((total_road_miles + detour_so_far) / self.mpg, 2),
                    'fuel_remaining_at_destination_gallons': 0.0,
                    'total_fuel_cost': cost_so_far,
                    'stops': selected_stops,
                    'algorithm': "Greedy Lookahead Corridor Search (Infeasible Route Detected)",
                    'message': (
                        f"Infeasible route: No reachable fuel station found between mile {gap_start} and {gap_limit} "
                        f"(including station detours). Next available station is at mile {next_mile}, "
                        f"creating a segment that exceeds the {usable_range:.0f}-mile vehicle range."
                    )
                }

            # Prefer stations making substantial forward progress (>= 40% of usable range from current pos)
            preferred = [
                item for item in preliminary_pool
                if item['station']['route_mile'] >= current_pos + (usable_range * 0.4)
            ]
            pool_to_rank = preferred if preferred else preliminary_pool

            # Rank candidate stations: lowest retail price + slight penalty for early stop or long detour
            def score_candidate(item):
                st = item['station']
                leg_dist = item['est_leg']
                remaining_margin = available_range - leg_dist
                early_penalty = 0.001 * max(0.0, remaining_margin - 100.0)
                detour_penalty = 0.002 * item['est_d_tot']
                return st['retail_price'] + early_penalty + detour_penalty

            ranked_items = sorted(pool_to_rank, key=score_candidate)

            # Evaluate top ranked candidates using exact routing detour
            best_chosen = None
            for item in ranked_items:
                cand = item['station']
                actual_d_to, actual_d_from, actual_d_tot = self.get_station_detour(
                    cand, routing_service=routing_service, cache=detour_cache
                )
                actual_leg = round(last_detour_from + (cand['route_mile'] - current_pos) + actual_d_to, 2)

                # Validate range on this leg with actual road driving distance and safety reserve
                if actual_leg > available_range or actual_leg > usable_range:
                    continue

                # Progress viability: can vehicle proceed after refilling to full?
                to_dest = round(actual_d_from + (total_road_miles - cand['route_mile']), 2)
                if to_dest <= usable_range:
                    best_chosen = (cand, actual_d_to, actual_d_from, actual_d_tot, actual_leg)
                    break

                can_reach_next = any(
                    s2['route_mile'] > cand['route_mile'] and
                    round(actual_d_from + (s2['route_mile'] - cand['route_mile']), 2) <= usable_range
                    for s2 in valid_candidates
                )
                if can_reach_next:
                    best_chosen = (cand, actual_d_to, actual_d_from, actual_d_tot, actual_leg)
                    break

            if best_chosen is None:
                # Infeasible: candidates in pool exceeded range or were dead ends with actual detours
                gap_start = round(current_pos, 1)
                gap_limit = round(current_pos + available_range, 1)
                purchased_so_far = round(sum(s['estimated_gallons_purchased'] for s in selected_stops), 2)
                cost_so_far = round(sum(s['estimated_cost'] for s in selected_stops), 2)
                detour_so_far = round(sum(s['detour_distance_miles'] for s in selected_stops), 2)

                return {
                    'feasible': False,
                    'requires_refueling': True,
                    'initial_fuel_assumption': (
                        f"Vehicle departs with {start_fuel_gallons:.1f} gallons ({initial_range:.1f} miles range)."
                    ),
                    'safety_reserve_miles': reserve_miles,
                    'usable_range_miles': usable_range,
                    'main_route_distance_miles': round(total_road_miles, 2),
                    'total_detour_distance_miles': detour_so_far,
                    'total_driving_distance_miles': round(total_road_miles + detour_so_far, 2),
                    'total_stops': len(selected_stops),
                    'total_fuel_gallons': purchased_so_far,
                    'total_gallons_purchased_at_stops': purchased_so_far,
                    'estimated_trip_consumption_gallons': round((total_road_miles + detour_so_far) / self.mpg, 2),
                    'fuel_remaining_at_destination_gallons': 0.0,
                    'total_fuel_cost': cost_so_far,
                    'stops': selected_stops,
                    'algorithm': "Greedy Lookahead Corridor Search (Infeasible Route Detected)",
                    'message': (
                        f"Infeasible route: Fuel stations were identified near mile {gap_start}-{gap_limit}, "
                        f"but the driving leg or subsequent segment exceeds the {usable_range:.0f}-mile vehicle range or available fuel."
                    )
                }

            best_station, best_d_to, best_d_from, best_d_tot, actual_leg_dist = best_chosen
            main_leg_dist = round(best_station['route_mile'] - current_pos, 2)

            # Leg calculations with actual driving distance
            fuel_consumed_on_leg = round(actual_leg_dist / self.mpg, 2)
            fuel_remaining_arrival = round(max(0.0, current_fuel - fuel_consumed_on_leg), 2)

            # Refill to full tank capacity (50.0 gal)
            gallons_purchased = round(tank_capacity_gallons - fuel_remaining_arrival, 2)
            gallons_purchased = max(0.0, min(tank_capacity_gallons, gallons_purchased))
            leg_cost = round(gallons_purchased * best_station['retail_price'], 2)
            post_refuel_fuel = tank_capacity_gallons

            stop_entry = {
                'stop_number': stop_index,
                'truckstop_id': best_station['truckstop_id'],
                'truckstop_name': best_station.get('truckstop_name', ''),
                'address': best_station.get('address', ''),
                'city': best_station.get('city', ''),
                'state': best_station.get('state', ''),
                'retail_price': best_station['retail_price'],
                'coordinates': best_station['coordinates'],
                'route_mile': best_station['route_mile'],
                'distance_from_route_miles': best_station.get('distance_from_route_miles', 0.0),
                'distance_from_previous_leg_miles': main_leg_dist,
                'detour_to_station_miles': best_d_to,
                'detour_from_station_miles': best_d_from,
                'detour_distance_miles': best_d_tot,
                'actual_leg_distance_miles': actual_leg_dist,
                'fuel_consumed_on_leg_gallons': fuel_consumed_on_leg,
                'fuel_remaining_on_arrival_gallons': fuel_remaining_arrival,
                'estimated_gallons_purchased': gallons_purchased,
                'fuel_level_after_refueling_gallons': post_refuel_fuel,
                'estimated_cost': leg_cost,
            }
            selected_stops.append(stop_entry)
            stop_index += 1

            # Update state for next segment
            current_pos = best_station['route_mile']
            current_fuel = post_refuel_fuel
            last_detour_from = best_d_from

        # Final leg to destination
        final_leg_main_distance = round(total_road_miles - current_pos, 2)
        final_leg_distance = round(last_detour_from + final_leg_main_distance, 2)
        final_leg_gallons = round(final_leg_distance / self.mpg, 2)

        # Validate final leg feasibility
        available_range = round(max(0.0, (current_fuel * self.mpg) - reserve_miles), 2)
        if final_leg_distance > available_range + 1e-4 or final_leg_distance > usable_range + 1e-4:
            detour_so_far = round(sum(s['detour_distance_miles'] for s in selected_stops), 2)
            return {
                'feasible': False,
                'requires_refueling': True,
                'initial_fuel_assumption': f"Vehicle departs with {start_fuel_gallons:.1f} gallons.",
                'safety_reserve_miles': reserve_miles,
                'usable_range_miles': usable_range,
                'main_route_distance_miles': round(total_road_miles, 2),
                'total_detour_distance_miles': detour_so_far,
                'total_driving_distance_miles': round(total_road_miles + detour_so_far, 2),
                'total_stops': len(selected_stops),
                'total_fuel_gallons': round(sum(s['estimated_gallons_purchased'] for s in selected_stops), 2),
                'total_gallons_purchased_at_stops': round(sum(s['estimated_gallons_purchased'] for s in selected_stops), 2),
                'estimated_trip_consumption_gallons': round((total_road_miles + detour_so_far) / self.mpg, 2),
                'fuel_remaining_at_destination_gallons': 0.0,
                'total_fuel_cost': round(sum(s['estimated_cost'] for s in selected_stops), 2),
                'stops': selected_stops,
                'algorithm': "Greedy Lookahead Corridor Search (Infeasible Final Leg Detected)",
                'message': (
                    f"Infeasible route: Final leg from mile {current_pos} to destination {total_road_miles} "
                    f"(including return detour of {last_detour_from:.2f} miles) is {final_leg_distance:.1f} miles, "
                    f"which exceeds the vehicle's usable range of {usable_range:.0f} miles."
                )
            }

        fuel_remaining_at_destination = round(max(0.0, current_fuel - final_leg_gallons), 2)

        # Validate that all legs strictly comply with usable vehicle range
        all_leg_distances = [s['actual_leg_distance_miles'] for s in selected_stops] + [final_leg_distance]
        if any(d > usable_range + 1e-4 for d in all_leg_distances):
            detour_so_far = round(sum(s['detour_distance_miles'] for s in selected_stops), 2)
            return {
                'feasible': False,
                'requires_refueling': True,
                'initial_fuel_assumption': f"Vehicle departs with {start_fuel_gallons:.1f} gallons.",
                'safety_reserve_miles': reserve_miles,
                'usable_range_miles': usable_range,
                'main_route_distance_miles': round(total_road_miles, 2),
                'total_detour_distance_miles': detour_so_far,
                'total_driving_distance_miles': round(total_road_miles + detour_so_far, 2),
                'total_stops': len(selected_stops),
                'total_fuel_gallons': round(sum(s['estimated_gallons_purchased'] for s in selected_stops), 2),
                'total_gallons_purchased_at_stops': round(sum(s['estimated_gallons_purchased'] for s in selected_stops), 2),
                'estimated_trip_consumption_gallons': round((total_road_miles + detour_so_far) / self.mpg, 2),
                'fuel_remaining_at_destination_gallons': 0.0,
                'total_fuel_cost': round(sum(s['estimated_cost'] for s in selected_stops), 2),
                'stops': selected_stops,
                'algorithm': "Greedy Lookahead Corridor Search (Leg Exceeded Range)",
                'message': f"Infeasible route: At least one leg exceeds usable vehicle range of {usable_range:.0f} miles."
            }

        main_route_distance_miles = round(total_road_miles, 2)
        total_detour_distance_miles = round(sum(s['detour_distance_miles'] for s in selected_stops), 2)
        total_driving_distance_miles = round(main_route_distance_miles + total_detour_distance_miles, 2)
        total_gallons_purchased = round(sum(s['estimated_gallons_purchased'] for s in selected_stops), 2)
        total_stop_refueling_cost = round(sum(s['estimated_cost'] for s in selected_stops), 2)
        trip_consumption = round(total_driving_distance_miles / self.mpg, 2)

        return {
            'feasible': True,
            'requires_refueling': len(selected_stops) > 0,
            'initial_fuel_assumption': (
                f"Vehicle departs with a full tank ({start_fuel_gallons:.1f} gallons, "
                f"{initial_range:.1f} miles range). Initial tank fuel cost is pre-existing and excluded "
                f"from stop-refueling expenditure. Vehicle refills to full capacity ({tank_capacity_gallons:.1f} gallons) "
                f"at each recommended fuel stop."
            ),
            'safety_reserve_miles': reserve_miles,
            'usable_range_miles': usable_range,
            'main_route_distance_miles': main_route_distance_miles,
            'total_detour_distance_miles': total_detour_distance_miles,
            'total_driving_distance_miles': total_driving_distance_miles,
            'total_stops': len(selected_stops),
            'total_fuel_gallons': total_gallons_purchased,
            'total_gallons_purchased_at_stops': total_gallons_purchased,
            'estimated_trip_consumption_gallons': trip_consumption,
            'fuel_remaining_at_destination_gallons': fuel_remaining_at_destination,
            'total_fuel_cost': total_stop_refueling_cost,
            'final_leg_main_route_distance_miles': final_leg_main_distance,
            'final_leg_detour_distance_miles': round(last_detour_from, 2),
            'final_leg_distance_miles': final_leg_distance,
            'final_leg_fuel_consumed_gallons': final_leg_gallons,
            'stops': selected_stops,
            'algorithm': (
                "Greedy Lookahead Corridor Search — iteratively selects the most cost-effective "
                "reachable fuel station along the route while guaranteeing every driving leg "
                f"(including station detours) remains within the {usable_range:.0f}-mile usable vehicle range."
            ),
            'message': (
                f"Route successfully planned: {len(selected_stops)} stop(s) recommended across "
                f"{total_driving_distance_miles:.1f} driving miles ({main_route_distance_miles:.1f} main route + "
                f"{total_detour_distance_miles:.1f} detour) with total stop-refueling expenditure of ${total_stop_refueling_cost:.2f}."
            )
        }
