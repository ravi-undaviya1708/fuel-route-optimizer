# Fuel Route Optimizer — Backend API (Day 1 & Day 2)

A high-performance Django REST Framework backend API that calculates driving routes within the United States, extracts road distance and GeoJSON geometry, manages OPIS fuel station records, and optimizes fuel stops for long-haul routes based on a 500-mile vehicle range and 10 MPG budget.

---

## Table of Contents

1. [Features](#features)
2. [Prerequisites](#prerequisites)
3. [Quickstart Setup](#quickstart-setup)
4. [Environment Variables](#environment-variables)
5. [Database Migrations](#database-migrations)
6. [Fuel Station Data Ingestion](#fuel-station-data-ingestion)
7. [Priority 1: Fuel Station Coordinate Enrichment](#priority-1-fuel-station-coordinate-enrichment)
8. [Priority 2: Route-Aware Fuel Stop Optimization](#priority-2-route-aware-fuel-stop-optimization)
9. [Running the Development Server](#running-the-development-server)
10. [API Documentation](#api-documentation)
    - [POST /api/route/ (Short Route — No Refueling)](#post-apiroute-short-route)
    - [POST /api/route/ (Long Route — Optimized Fuel Stops)](#post-apiroute-long-route)
    - [GET /api/health/](#get-apihealth)
    - [GET /api/fuel-stations/](#get-apifuel-stations)
11. [Postman Demo Steps](#postman-demo-steps)
12. [Running the Test Suite](#running-the-test-suite)
13. [Architecture & Design Decisions](#architecture--design-decisions)

---

## Features

- **Route Calculation API**: Accepts US start and destination addresses, validates locations, and returns road distance (miles and km), driving duration, and GeoJSON `LineString` route geometry suitable for map rendering (e.g. Mapbox, Leaflet, Google Maps).
- **Route-Aware Fuel Stop Optimization**:
  - Dynamically projects and matches fuel stations to the actual driving polyline using a configurable corridor proximity threshold (default: 15 miles).
  - Calculates road distance along polyline segments, not straight-line distance between cities.
  - Ensures each travel segment strictly respects the vehicle's 500-mile maximum range.
  - Selects cost-effective fuel stops along the corridor, returning exact locations, prices, distances between stops, estimated gallons purchased, and total fuel expenditure.
- **Resumable, Rate-Conscious Coordinate Enrichment**:
  - Offline dataset integration (`us_cities.csv`) geocodes over 98% of stations instantly with zero external network calls.
  - Persistent local coordinate cache (`station_coordinate_cache.json`) for Canadian cities and custom overrides.
  - Rate-limited fallback geocoding (1.0s polite delay) with exponential backoff retries.
  - 100% persistent coordinates (6,738 / 6,738 stations with coordinates).
- **External Services Integration**:
  - **OpenStreetMap Nominatim**: For address geocoding with strict US boundary filtering, custom User-Agent headers, and rate-limit compliance.
  - **OSRM (Open Source Routing Machine)**: For driving route computation and high-resolution geometry.
- **Dual-Layer Caching**: Normalized SHA-256 hashed keys for geocoding and route computations, eliminating redundant upstream calls and responding in milliseconds.
- **Robust CSV Ingestion**: High-throughput ingestion of 8,151 OPIS fuel station records into SQLite using transactional batch operations (`bulk_create` / `bulk_update`), exact 8-decimal price precision, and 100% idempotent repeat imports.

---

## Prerequisites

- **Python**: 3.10, 3.11, 3.12, or 3.13 (Tested with Python 3.13.7)
- **Git**
- **SQLite 3** (built into Python)

---

## Quickstart Setup

### 1. Clone & Navigate to Repository

```bash
git clone <repo-url> fuel-route-optimizer
cd fuel-route-optimizer
```

### 2. Create and Activate Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Environment Variables

Copy the example environment file:

```bash
cp .env.example .env
```

Configuration in `.env`:

```env
# Django Configuration
DJANGO_SECRET_KEY=dev-secret-key-fuel-optimizer-local-testing-2026
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,testserver

# Geocoding & Routing Services
NOMINATIM_USER_AGENT=FuelRouteOptimizer/1.0 (Macintosh; Intel Mac OS X 10_15_7; fuel-route-optimizer)
NOMINATIM_BASE_URL=https://nominatim.openstreetmap.org
OSRM_BASE_URL=https://router.project-osrm.org

# Cache & Performance
CACHE_TTL_SECONDS=86400
HTTP_TIMEOUT_SECONDS=10
```

---

## Database Migrations

Apply the database migrations:

```bash
python manage.py migrate
```

---

## Fuel Station Data Ingestion

The CSV file `data/fuel-prices-for-be-assessment.csv` contains 8,151 OPIS fuel price records.

Run the ingestion management command:

```bash
python manage.py import_fuel_prices data/fuel-prices-for-be-assessment.csv
```

### Ingestion Details & Deduplication Policy:
- **Columns handled**: `OPIS Truckstop ID`, `Truckstop Name`, `Address`, `City`, `State`, `Rack ID`, `Retail Price`.
- **Deduplication**: When a truckstop ID appears multiple times with differing prices, the importer deterministically selects the lowest retail price for vehicle route optimization.
- **Idempotency**: Running the import command repeatedly produces **0 new records**, skips unchanged records, and preserves database consistency.

---

## Priority 1: Fuel Station Coordinate Enrichment

### Why the Health Endpoint Previously Reported 0 Coordinates
1. The OPIS CSV provides street/highway intersection addresses (e.g. `I-44, EXIT 283 & US-69`) without latitude or longitude columns.
2. At initial ingestion, `FuelStation` records were created with `latitude=None` and `longitude=None`.
3. Standard US Census Bureau address geocoding requires street numbers (e.g. `123 Main St`) and fails on highway exit descriptions.

### How it was Resolved
The enrichment workflow was upgraded with a multi-stage, rate-conscious architecture:
1. **Stage 1 (Offline Geographic Dataset)**: Utilizes `data/us_cities.csv` (29,738 US cities) and `data/station_coordinate_cache.json` (Canadian cities and custom overrides). Matches station city and state pairs instantly with **zero network requests** and zero API quota consumption.
2. **Stage 2 (Rate-Conscious Fallback Geocoder)**: For any remaining unmapped locations, groups queries by unique `(city, state)`, enforces a 1.0s delay between requests, applies exponential backoff retries, and saves newly discovered coordinates to the local cache.
3. **Persistent SQLite Storage**: Bulk updates `FuelStation` table transactionally.

### Running the Coordinate Enrichment Command:

```bash
python manage.py geocode_stations
```

Output:
```text
=================================================================
       FUEL STATION COORDINATE ENRICHMENT PROCESS
=================================================================
Total stations in database:            6738
Stations currently without coordinates: 6738

Stage 1: Running offline dataset matching (us_cities.csv + cache)...
  -> Enriched 6738 stations instantly from offline datasets.
  -> Remaining stations without coordinates: 0

=================================================================
           COORDINATE ENRICHMENT SUMMARY
=================================================================
Total stations in DB:                  6738
Stations now WITH coordinates:         6738 (100.0%)
Stations remaining for future retry:   0
=================================================================
```

Verification via `/api/health/`:
```json
{
  "status": "healthy",
  "service": "fuel-route-optimizer",
  "database": {
    "connected": true,
    "error": null,
    "total_fuel_stations": 6738,
    "stations_with_coordinates": 6738
  },
  "version": "1.0.0 (Day 1)"
}
```

---

## Priority 2: Route-Aware Fuel Stop Optimization

### 1. Vehicle & Fuel Accounting Assumptions
- **Maximum Range**: 500.0 miles.
- **Fuel Efficiency**: 10.0 MPG (Miles Per Gallon).
- **Fuel Tank Capacity**: 50.0 gallons (`500.0 miles / 10.0 MPG`).
- **Initial Fuel Assumption**: The vehicle departs with a **full tank of 50.0 gallons** (500.0 miles of range). Initial tank fuel is pre-existing and excluded from stop-refueling expenditure. Configurable via `initial_fuel_gallons`.
- **Refueling Policy**: At each recommended fuel stop, the vehicle refills to full capacity (50.0 gallons).
- **Fuel State Tracking**:
  - Segment consumption = `segment_miles / 10.0 MPG`.
  - Fuel remaining on arrival = `previous_fuel - segment_consumption`.
  - Refill purchase = `tank_capacity - fuel_remaining_on_arrival`. Never exceeds tank capacity (50.0 gallons).
  - Post-refuel fuel level = 50.0 gallons.
  - Final leg to destination arrives with `fuel_remaining_at_destination_gallons = 50.0 - final_leg_consumption`.
- **Separation of Consumption vs. Purchases**:
  - `estimated_trip_consumption_gallons`: Total fuel burned across the entire trip (`route_distance / 10.0 MPG`).
  - `estimated_gallons_purchased`: Fuel purchased at each individual stop (`<= 50.0` gallons).
  - `total_gallons_purchased_at_stops` / `total_fuel_gallons`: Total gallons purchased at refueling stops.
  - `total_fuel_cost`: Sum of stop refueling costs (`estimated_gallons_purchased * retail_price`).

### 2. Corridor Extraction & Road Distance Mapping
- **Calibrated Road Distance**: Polyline segment lengths are computed using great-circle Haversine distance and calibrated to match OSRM's total driving road distance (not straight-line flight distance).
- **Perpendicular Projection**: Stations within the bounding box are projected onto the nearest polyline segment. If the perpendicular distance is within `max_proximity_miles` (default: 15.0 miles), the station is considered a viable candidate along the road corridor.
- **Station Route Mile**: Each station is assigned its exact mileage along the driving route from the start.

### 3. Stop Selection Algorithm (Greedy Lookahead Search)
- **Algorithm Type**: Greedy Lookahead Corridor Search.
- **Guarantee**: Every travel leg `(start -> stop_1)`, `(stop_i -> stop_{i+1})`, and `(stop_k -> destination)` is strictly `<= 500.0 miles`.
- **Cost Effectiveness**: Within the reachable forward window, the algorithm prioritizes stations with the lowest retail price per gallon while ensuring continuous forward progress and lookahead feasibility.
- **Short Routes (`<= 500 miles`)**: Returns `requires_refueling = false`, `total_stops = 0`, and `$0.00` fuel expenditure.
- **Infeasible Routes (Gaps `> 500 miles`)**: If no fuel station exists in the reachable range, the algorithm immediately reports `feasible = false` with an explanatory message identifying the exact mileage gap.

---

## Running the Development Server

Start the local server:

```bash
python manage.py runserver 127.0.0.1:8000
```

---

## API Documentation

### POST `/api/route/` (Short Route)

#### Request Body:
```json
{
  "start": "New York, NY",
  "destination": "Philadelphia, PA"
}
```

#### Response (`200 OK`):
```json
{
  "start": {
    "query": "New York, NY",
    "name": "New York, United States",
    "coordinates": [-74.0060152, 40.7127281]
  },
  "destination": {
    "query": "Philadelphia, PA",
    "name": "Philadelphia, Philadelphia County, Pennsylvania, United States",
    "coordinates": [-75.1635262, 39.9527237]
  },
  "distance": {
    "miles": 94.64,
    "kilometers": 152.3,
    "meters": 152300.7
  },
  "duration_seconds": 7585.5,
  "duration_formatted": {
    "hours": 2,
    "minutes": 6,
    "seconds": 25
  },
  "geometry": {
    "type": "LineString",
    "coordinates": [[-74.006015, 40.712728], [-75.163526, 39.952724]]
  },
  "fuel_stops": {
    "feasible": true,
    "requires_refueling": false,
    "initial_fuel_assumption": "Vehicle departs with 50.0 gallons (500.0 miles range). Initial tank fuel is pre-existing; no refueling stops required.",
    "total_stops": 0,
    "total_fuel_gallons": 0.0,
    "total_gallons_purchased_at_stops": 0.0,
    "estimated_trip_consumption_gallons": 9.46,
    "fuel_remaining_at_destination_gallons": 40.54,
    "total_fuel_cost": 0.0,
    "final_leg_distance_miles": 94.64,
    "final_leg_fuel_consumed_gallons": 9.46,
    "stops": [],
    "algorithm": "Greedy Lookahead Corridor Search — verified route distance is within starting fuel range.",
    "message": "Route distance (94.6 miles) is within vehicle range. No refueling needed."
  },
  "metadata": {
    "routing_provider": "OSRM (Open Source Routing Machine)",
    "geocoding_provider": "OpenStreetMap Nominatim",
    "cached": {
      "start_geocoding": true,
      "destination_geocoding": true,
      "route": true
    },
    "vehicle_specs": {
      "max_range_miles": 500,
      "mpg": 10,
      "tank_capacity_gallons": 50.0,
      "initial_fuel_gallons": 50.0,
      "estimated_trip_consumption_gallons": 9.46,
      "estimated_fuel_gallons": 9.46,
      "requires_refueling": false,
      "feasible": true,
      "candidate_stations_in_corridor": 38,
      "proximity_threshold_miles": 15.0
    }
  }
}
```

---

### POST `/api/route/` (Long Route)

#### Request Body:
```json
{
  "start": "New York, NY",
  "destination": "Chicago, IL",
  "max_proximity_miles": 15.0,
  "initial_fuel_gallons": 50.0
}
```

#### Response (`200 OK`):
```json
{
  "start": {
    "query": "New York, NY",
    "name": "New York, United States",
    "coordinates": [-74.0060152, 40.7127281]
  },
  "destination": {
    "query": "Chicago, IL",
    "name": "Chicago, Cook County, Illinois, United States",
    "coordinates": [-87.6244212, 41.8755616]
  },
  "distance": {
    "miles": 790.57,
    "kilometers": 1272.3,
    "meters": 1272305.4
  },
  "duration_seconds": 44920.0,
  "duration_formatted": {
    "hours": 12,
    "minutes": 28,
    "seconds": 40
  },
  "geometry": {
    "type": "LineString",
    "coordinates": [[-74.006015, 40.712728], [-87.624421, 41.875562]]
  },
  "fuel_stops": {
    "feasible": true,
    "requires_refueling": true,
    "initial_fuel_assumption": "Vehicle departs with a full tank (50.0 gallons, 500.0 miles range). Initial tank fuel cost is pre-existing and excluded from stop-refueling expenditure. Vehicle refills to full capacity (50.0 gallons) at each recommended fuel stop.",
    "total_stops": 1,
    "total_fuel_gallons": 40.03,
    "total_gallons_purchased_at_stops": 40.03,
    "estimated_trip_consumption_gallons": 79.06,
    "fuel_remaining_at_destination_gallons": 10.97,
    "total_fuel_cost": 122.72,
    "final_leg_distance_miles": 390.29,
    "final_leg_fuel_consumed_gallons": 39.03,
    "stops": [
      {
        "stop_number": 1,
        "truckstop_id": 72782,
        "truckstop_name": "SHEETZ #791",
        "address": "I-76, Exit 57",
        "city": "North Jackson",
        "state": "OH",
        "retail_price": 3.06566666,
        "coordinates": [-80.86225, 41.088044],
        "route_mile": 400.28,
        "distance_from_previous_leg_miles": 400.28,
        "distance_from_route_miles": 2.13,
        "fuel_consumed_on_leg_gallons": 40.03,
        "fuel_remaining_on_arrival_gallons": 9.97,
        "estimated_gallons_purchased": 40.03,
        "fuel_level_after_refueling_gallons": 50.0,
        "estimated_cost": 122.72
      }
    ],
    "algorithm": "Greedy Lookahead Corridor Search — iteratively selects the most cost-effective reachable fuel station along the route while guaranteeing every segment remains within the 500-mile vehicle range.",
    "message": "Route successfully planned: 1 stop(s) recommended across 790.6 miles with total stop-refueling expenditure of $122.72."
  },
  "metadata": {
    "routing_provider": "OSRM (Open Source Routing Machine)",
    "geocoding_provider": "OpenStreetMap Nominatim",
    "cached": {
      "start_geocoding": true,
      "destination_geocoding": true,
      "route": true
    },
    "vehicle_specs": {
      "max_range_miles": 500,
      "mpg": 10,
      "tank_capacity_gallons": 50.0,
      "initial_fuel_gallons": 50.0,
      "estimated_trip_consumption_gallons": 79.06,
      "estimated_fuel_gallons": 79.06,
      "requires_refueling": true,
      "feasible": true,
      "candidate_stations_in_corridor": 130,
      "proximity_threshold_miles": 15.0
    }
  }
}
```

---

### GET `/api/health/`

Returns health status and database statistics:

```json
{
  "status": "healthy",
  "service": "fuel-route-optimizer",
  "database": {
    "connected": true,
    "error": null,
    "total_fuel_stations": 6738,
    "stations_with_coordinates": 6738
  },
  "version": "1.0.0 (Day 1)"
}
```

---

### GET `/api/fuel-stations/`

Paginated station explorer with filtering:
`GET /api/fuel-stations/?state=TX&search=Pilot&ordering=retail_price`

---

## Postman Demo Steps

1. **Start the Django Development Server**:
   ```bash
   source .venv/bin/activate
   python manage.py runserver 127.0.0.1:8000
   ```
2. **Health Check**:
   - Method: `GET`
   - URL: `http://127.0.0.1:8000/api/health/`
   - Verify `stations_with_coordinates: 6738`.
3. **Short Route Demo (< 500 miles, No Fuel Stop Needed)**:
   - Method: `POST`
   - URL: `http://127.0.0.1:8000/api/route/`
   - Headers: `Content-Type: application/json`
   - Body:
     ```json
     {
       "start": "New York, NY",
       "destination": "Philadelphia, PA"
     }
     ```
   - Verify `requires_refueling: false`, `total_stops: 0`.
4. **Long Route Demo (> 500 miles, Fuel Stop Optimization)**:
   - Method: `POST`
   - URL: `http://127.0.0.1:8000/api/route/`
   - Headers: `Content-Type: application/json`
   - Body:
     ```json
     {
       "start": "New York, NY",
       "destination": "Chicago, IL",
       "max_proximity_miles": 15.0
     }
     ```
   - Verify `requires_refueling: true`, `total_stops: 1`, station location, gallons, cost, and GeoJSON geometry.
5. **Cross-Country Route Demo (> 1,400 miles, Multi-Stop Optimization)**:
   - Method: `POST`
   - URL: `http://127.0.0.1:8000/api/route/`
   - Body:
     ```json
     {
       "start": "Los Angeles, CA",
       "destination": "Dallas, TX"
     }
     ```
   - Verify 3 recommended stops along I-10 / I-20 with segment distances `<= 500 miles`.

---

## Running the Test Suite

The test suite contains **28 automated tests** covering all routing, geocoding, caching, station enrichment, and fuel stop optimization scenarios:

```bash
# Run with pytest:
pytest -v

# Or run with Django test runner:
python manage.py test fuel_optimizer
```

All 28 tests execute in `< 0.20s` with zero external network dependencies (all external HTTP calls mocked).

---

## Architecture & Design Decisions

```text
POST /api/route/
      │
      ├───> Nominatim Geocoding Service (Cached SHA-256)
      │
      ├───> OSRM Driving Route Service (Cached SHA-256)
      │
      ├───> Polyline Road Calibration (Haversine segment integration)
      │
      ├───> Spatial Corridor Search (Bounding box filter + perpendicular projection)
      │
      └───> RouteFuelOptimizer (Greedy Lookahead Search)
               ├── Checks Initial Fuel Range (500 miles)
               ├── Iteratively selects cheapest reachable forward station
               └── Generates detailed stop plan & trip expenditure
```
