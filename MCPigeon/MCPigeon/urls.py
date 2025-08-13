# urls.py
from django.urls import path, include
from django.contrib import admin  # import

urlpatterns = [
    path("", include('mcp_server.urls')),
    path("campaigns/", include("campaigns.urls")),
    path("admin/", admin.site.urls),
]
