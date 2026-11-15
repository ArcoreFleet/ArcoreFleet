from django.contrib import admin

from .models import AuthorityConfigVersion


@admin.register(AuthorityConfigVersion)
class AuthorityConfigVersionAdmin(admin.ModelAdmin):
    list_display = ('version', 'authority_id', 'environment', 'tenant', 'created_by', 'created_at')
    readonly_fields = [f.name for f in AuthorityConfigVersion._meta.fields]

    def has_add_permission(self, request):
        return False  # publish from the ArcoreHub page, which signs

    def has_change_permission(self, request, obj=None):
        return False  # versions are immutable
