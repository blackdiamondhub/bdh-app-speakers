from django.urls import path

from . import views

urlpatterns = [
    path('', views.speakers_view, name='speakers'),
    path('state/', views.state, name='speakers_state'),
    path('action/', views.action, name='speakers_action'),
    path('art/', views.art, name='speakers_art'),
]