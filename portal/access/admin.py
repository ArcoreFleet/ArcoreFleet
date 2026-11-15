from django.contrib import admin

from .models import Access, ApiKey, Application, PortalSettings


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ('name', 'hostname', 'is_active', 'admin_only', 'sort_order')
    list_editable = ('is_active', 'admin_only', 'sort_order')


@admin.register(Access)
class AccessAdmin(admin.ModelAdmin):
    list_display = ('user', 'all_apps')
    filter_horizontal = ('apps',)


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'hint', 'created_at', 'last_used_at', 'revoked_at')
    readonly_fields = ('hint', 'key_hash', 'created_at', 'last_used_at')


admin.site.register(PortalSettings)
