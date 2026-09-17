from django.urls import path

from . import views

app_name = "production"

urlpatterns = [
    # Raw materials
    path("materials/", views.MaterialListView.as_view(), name="material_list"),
    path("materials/add/", views.MaterialCreateView.as_view(), name="material_create"),
    path("materials/low/", views.LowMaterialView.as_view(), name="material_low"),
    path("materials/movements/", views.MaterialMovementListView.as_view(),
         name="material_movements"),
    path("materials/<int:pk>/", views.MaterialDetailView.as_view(),
         name="material_detail"),
    path("materials/<int:pk>/edit/", views.MaterialUpdateView.as_view(),
         name="material_update"),
    path("materials/<int:pk>/receive/", views.material_receive,
         name="material_receive"),
    path("materials/<int:pk>/adjust/", views.material_adjust,
         name="material_adjust"),
    # Recipes
    path("recipes/", views.RecipeListView.as_view(), name="recipe_list"),
    path("recipes/<int:pk>/", views.recipe_edit, name="recipe_edit"),
    # Production runs
    path("runs/", views.RunListView.as_view(), name="run_list"),
    path("runs/new/", views.run_create, name="run_create"),
    path("runs/<int:pk>/", views.RunDetailView.as_view(), name="run_detail"),
    path("runs/<int:pk>/reverse/", views.run_reverse, name="run_reverse"),
    # "We are running out - please make more"
    path("requests/", views.request_list, name="request_list"),
    path("requests/new/", views.request_create, name="request_create"),
    path("requests/<int:pk>/respond/", views.request_respond,
         name="request_respond"),
    path("requests/<int:pk>/cancel/", views.request_cancel,
         name="request_cancel"),
    # Used by the run form as you type
    path("api/plan/", views.plan_api, name="plan_api"),
]
