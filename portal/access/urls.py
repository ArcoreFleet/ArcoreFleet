from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('operations/', views.operations, name='operations'),
    path('settings/', views.fleet_settings, name='fleet_settings'),
    path('operations/poll/', views.poll_operations, name='poll_operations'),
    path('operations/interval/', views.poll_interval, name='poll_interval'),
    path('operations/servers/new/', views.manage_server, name='add_server'),
    path('operations/servers/<int:pk>/', views.manage_server, name='edit_server'),
    path('auth/check', views.auth_check, name='auth_check'),
    path('keys/', views.api_keys, name='api_keys'),
    path('api/keys/resolve', views.resolve_api_key, name='resolve_api_key'),
    path('healthz', views.healthz, name='healthz'),
    path('logout/', views.logout_view, name='logout'),
    path('manage/users/', views.manage_users, name='manage_users'),
    path('manage/apps/', views.manage_apps, name='manage_apps'),
    path('manage/apps/<int:pk>/', views.edit_app, name='edit_app'),
    path('manage/apps/<int:pk>/delete/', views.delete_app, name='delete_app'),
]
