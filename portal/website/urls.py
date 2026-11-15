from django.urls import path

from . import views

urlpatterns = [
    path('website/', views.queue, name='website_queue'),
    path('website/refresh/', views.refresh, name='website_refresh'),
]
