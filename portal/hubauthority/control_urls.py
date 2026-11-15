"""URLs for the hub-authority deployment: the control plane ArcoreHub talks to over
pinned TLS. Nothing else from the portal is served there."""

from django.urls import path

from . import views

urlpatterns = [
    path('healthz', views.control_healthz),
    path('v1/config', views.control_config),
]
