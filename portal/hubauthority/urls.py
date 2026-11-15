from django.urls import path

from . import views

urlpatterns = [
    path('hub/', views.hub, name='hub'),
    path('hub/profile', views.hub_profile, name='hub_profile'),
    path('hub/provisioning/', views.hub_provisioning, name='hub_provisioning'),
    path('hub/provisioning/<int:pk>/revoke/', views.hub_revoke, name='hub_revoke'),
]
