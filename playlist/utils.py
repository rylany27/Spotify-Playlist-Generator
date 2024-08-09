import json
from django.conf import settings


def get_recommendations(query):
	url = 'https://api.spotify.com/v1/recommendations'
	query = json.loads(query)
	#TODO: handle artists later
	params = {
		'seed_genres': query['genres'],
		'seed_tracks': query['tracks'],
	}

