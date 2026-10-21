from django.db import models


class FuelStation(models.Model):
    """
    Represents a fuel station / truck stop with retail pricing and coordinates.
    """
    truckstop_id = models.IntegerField(
        unique=True,
        db_index=True,
        help_text="Unique OPIS Truckstop ID"
    )
    truckstop_name = models.CharField(
        max_length=255,
        help_text="Name or brand of the truckstop"
    )
    address = models.CharField(
        max_length=255,
        help_text="Street address or highway intersection"
    )
    city = models.CharField(
        max_length=128,
        db_index=True,
        help_text="City where station is located"
    )
    state = models.CharField(
        max_length=10,
        db_index=True,
        help_text="Two-letter state or province code (e.g. TX, CA)"
    )
    rack_id = models.IntegerField(
        help_text="OPIS Rack identifier"
    )
    retail_price = models.DecimalField(
        max_digits=12,
        decimal_places=8,
        db_index=True,
        help_text="Retail fuel price per gallon"
    )
    latitude = models.FloatField(
        null=True,
        blank=True,
        db_index=True,
        help_text="WGS84 latitude coordinate"
    )
    longitude = models.FloatField(
        null=True,
        blank=True,
        db_index=True,
        help_text="WGS84 longitude coordinate"
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when station was created"
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when station was last updated"
    )

    class Meta:
        db_table = 'fuel_stations'
        verbose_name = 'Fuel Station'
        verbose_name_plural = 'Fuel Stations'
        ordering = ['state', 'retail_price']
        indexes = [
            models.Index(fields=['state', 'retail_price'], name='idx_state_price'),
            models.Index(fields=['latitude', 'longitude'], name='idx_lat_lon'),
        ]

    def __str__(self):
        return f"{self.truckstop_name} ({self.truckstop_id}) - {self.city}, {self.state} [${self.retail_price:.3f}]"

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None
