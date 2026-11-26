from django.urls import path

from .views import reference_systems

app_name = "georef"

urlpatterns = [
    path("reference-systems/", reference_systems, name="reference-systems"),
]
