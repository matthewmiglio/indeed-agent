"""Commute distance estimation using geopy.

Geocodes addresses via Nominatim, calculates straight-line distance with
geodesic, and estimates commute time at a configurable average speed.
Results are cached to data/geocode_cache.json to avoid redundant API calls.
"""

import json
import os
import time
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "geocode_cache.json")
_geolocator = Nominatim(user_agent="openclaw-indeed-agent")

# In-memory cache (loaded from file on first use)
_cache = None


def _load_cache() -> dict:
    """Load the geocode cache from disk."""
    global _cache
    if _cache is not None:
        return _cache
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r") as f:
            _cache = json.load(f)
    else:
        _cache = {}
    return _cache


def _save_cache():
    """Write the geocode cache to disk."""
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(_cache, f, indent=2)


def geocode(location_str: str) -> tuple[float, float] | None:
    """Geocode a location string to (lat, lng) coordinates.

    Uses Nominatim with a file-backed cache. Sleeps 1.1s between API calls
    to respect Nominatim's rate limit (1 req/sec).

    Returns:
        (latitude, longitude) tuple, or None if geocoding fails.
    """
    if not location_str or not location_str.strip():
        return None

    cache = _load_cache()
    cache_key = location_str.strip().lower()

    # Check cache
    if cache_key in cache:
        val = cache[cache_key]
        if val is None:
            return None
        return tuple(val)

    # Query Nominatim
    try:
        time.sleep(1.1)  # Rate limit compliance
        result = _geolocator.geocode(location_str, timeout=10)
    except (GeocoderTimedOut, GeocoderServiceError) as e:
        print(f"  [distance] Geocoding error for '{location_str[:50]}': {e}")
        return None

    if result is None:
        # Cache the miss so we don't retry
        cache[cache_key] = None
        _save_cache()
        print(f"  [distance] Could not geocode: '{location_str[:50]}'")
        return None

    coords = (result.latitude, result.longitude)
    cache[cache_key] = list(coords)
    _save_cache()
    print(f"  [distance] Geocoded '{location_str[:40]}' -> ({coords[0]:.4f}, {coords[1]:.4f})")
    return coords


def is_remote(location_str: str) -> bool:
    """Check if a job location indicates remote work."""
    if not location_str:
        return False
    text = location_str.strip().lower()
    remote_indicators = ("remote", "work from home", "anywhere")
    return any(indicator in text for indicator in remote_indicators)


def calc_commute_minutes(origin: tuple[float, float], destination: tuple[float, float],
                         avg_speed_mph: float = 30.0) -> float:
    """Estimate one-way commute time in minutes using straight-line distance.

    Args:
        origin: (lat, lng) of home address.
        destination: (lat, lng) of job location.
        avg_speed_mph: Assumed average driving speed.

    Returns:
        Estimated commute time in minutes.
    """
    distance_miles = geodesic(origin, destination).miles
    return (distance_miles / avg_speed_mph) * 60


def filter_jobs_by_commute(jobs: list[dict], home_coords: tuple[float, float],
                           max_minutes: float,
                           avg_speed_mph: float = 30.0) -> tuple[list[dict], list[dict]]:
    """Filter a list of jobs by estimated commute time.

    Geocodes each job's location and calculates commute from home_coords.
    Attaches distance_miles, commute_minutes, and distance_status to each job dict.

    Args:
        jobs: List of job dicts with a 'location' field.
        home_coords: (lat, lng) of the user's home.
        max_minutes: Maximum acceptable commute in minutes.
        avg_speed_mph: Average speed assumption for commute estimate.

    Returns:
        (passed_jobs, filtered_jobs) — two lists.
    """
    print(f"  [distance] Filtering {len(jobs)} jobs (max commute: {max_minutes} min)")

    passed = []
    filtered = []

    # Pre-geocode unique locations to minimize API calls
    unique_locations = set()
    for job in jobs:
        loc = job.get("location", "")
        if loc and not is_remote(loc):
            unique_locations.add(loc)

    print(f"  [distance] {len(unique_locations)} unique non-remote locations to geocode")
    coord_map = {}
    for loc in unique_locations:
        coord_map[loc] = geocode(loc)

    # Filter each job
    for job in jobs:
        location = job.get("location", "")

        # Remote jobs always pass
        if is_remote(location):
            job["distance_miles"] = None
            job["commute_minutes"] = None
            job["distance_status"] = "remote"
            passed.append(job)
            continue

        # Empty location — pass by default
        if not location:
            job["distance_miles"] = None
            job["commute_minutes"] = None
            job["distance_status"] = "no_location"
            passed.append(job)
            continue

        # Geocode the job location
        job_coords = coord_map.get(location)
        if job_coords is None:
            # Geocoding failed — let job pass
            job["distance_miles"] = None
            job["commute_minutes"] = None
            job["distance_status"] = "geocode_failed"
            passed.append(job)
            continue

        # Calculate commute
        distance_miles = geodesic(home_coords, job_coords).miles
        commute_min = (distance_miles / avg_speed_mph) * 60

        job["distance_miles"] = round(distance_miles, 1)
        job["commute_minutes"] = round(commute_min, 1)

        if commute_min <= max_minutes:
            job["distance_status"] = "in_range"
            passed.append(job)
            print(f"    PASS  {location[:40]} — {distance_miles:.1f} mi, ~{commute_min:.0f} min")
        else:
            job["distance_status"] = "too_far"
            filtered.append(job)
            print(f"    SKIP  {location[:40]} — {distance_miles:.1f} mi, ~{commute_min:.0f} min (>{max_minutes})")

    print(f"  [distance] Result: {len(passed)} passed, {len(filtered)} filtered out")
    return passed, filtered
