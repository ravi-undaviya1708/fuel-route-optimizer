from django.urls import path
from fuel_optimizer.views import RouteView, HealthCheckView, FuelStationListView

app_name = 'fuel_optimizer'

urlpatterns = [
    path('health/', HealthCheckView.as_view(), name='health'),
    path('route/', RouteView.as_view(), name='route'),
    path('fuel-stations/', FuelStationListView.as_view(), name='fuel-station-list'),
]
