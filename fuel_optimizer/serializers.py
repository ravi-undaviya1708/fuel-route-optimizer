from rest_framework import serializers
from fuel_optimizer.models import FuelStation


class RouteRequestSerializer(serializers.Serializer):
    """Validates the route request payload."""
    start = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        max_length=255,
        help_text="Starting address or city/state within the USA (e.g. 'New York, NY')"
    )
    destination = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        max_length=255,
        help_text="Destination address or city/state within the USA (e.g. 'Philadelphia, PA')"
    )
    max_proximity_miles = serializers.FloatField(
        required=False,
        default=15.0,
        min_value=1.0,
        max_value=100.0,
        help_text="Maximum perpendicular distance (in miles) from route to search for fuel stations (default: 15.0)"
    )
    initial_fuel_gallons = serializers.FloatField(
        required=False,
        default=50.0,
        min_value=0.0,
        max_value=50.0,
        help_text="Initial fuel in tank upon departure in gallons (default: 50.0 = full tank)"
    )
    safety_reserve_miles = serializers.FloatField(
        required=False,
        default=0.0,
        min_value=0.0,
        max_value=100.0,
        help_text="Safety fuel reserve distance in miles so vehicle does not arrive near empty (default: 0.0)"
    )

    def validate_start(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Start location cannot be blank.")
        return value.strip()

    def validate_destination(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Destination location cannot be blank.")
        return value.strip()


class FuelStationSerializer(serializers.ModelSerializer):
    """Serializes FuelStation records."""
    has_coordinates = serializers.BooleanField(read_only=True)

    class Meta:
        model = FuelStation
        fields = [
            'id',
            'truckstop_id',
            'truckstop_name',
            'address',
            'city',
            'state',
            'rack_id',
            'retail_price',
            'latitude',
            'longitude',
            'has_coordinates',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields
