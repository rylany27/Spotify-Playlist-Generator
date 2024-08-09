from django.urls import path
from . import views

urlpatterns = [
	path('', views.login_page, name='login_page'),
	path('login/', views.login, name='login'),
	path('callback/', views.callback, name='callback'),
	path('logout/', views.logout, name='logout'),
    path('generate/', views.generate_response, name='generate_response'),
]