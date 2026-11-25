"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.urls import path

from .views import (
    MeasurementListView,
    measurement_photo,
    point_velocity,
    survey_years,
)

app_name = "surveys"

urlpatterns = [
    path("years/", survey_years, name="years"),
    path("measurements/", MeasurementListView.as_view(), name="measurements"),
    path("points/<str:label>/velocity/", point_velocity, name="point-velocity"),
    path("photos/<int:pk>/", measurement_photo, name="measurement-photo"),
]
