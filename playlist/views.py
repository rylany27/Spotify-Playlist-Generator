import os
import string
import random
import base64
import json
import time
import requests
import logging
from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.db import transaction
from django.shortcuts import render, redirect
from django.http import HttpResponseBadRequest
from .models import Artist, Track
import google.generativeai as genai
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from Levenshtein import distance
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

genai.configure(api_key=settings.GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

logging.basicConfig(level=logging.INFO)

def check_token_validity(token_info):
	sp = spotipy.Spotify(auth=token_info['access_token'])
	try:
		sp.current_user()
		return True
	except spotipy.SpotifyException:
		return False

def refresh_token(token_info):
	sp_oauth = SpotifyOAuth(
		client_id=settings.SPOTIFY_CLIENT_ID,
		client_secret=settings.SPOTIFY_CLIENT_SECRET,
		redirect_uri=settings.SPOTIFY_REDIRECT_URI,
		scope=settings.SPOTIFY_SCOPE,
	)
	token_info = sp_oauth.refresh_access_token(token_info['refresh_token'])
	return token_info

def get_spotify_client(request):
	token_info = request.session.get('token_info', None)
	if not token_info:
		return None

	if not check_token_validity(token_info):
		token_info = refresh_token(token_info)
		request.session['token_info'] = token_info

	sp = spotipy.Spotify(auth=token_info['access_token'])
	return sp

def login_page(request):
	token_info = request.session.get('token_info', None)
	if token_info and check_token_validity(token_info):
		return redirect('generate_response')
	return render(request, 'playlist/login_page.html')

def login(request):
	token_info = request.session.get('token_info', None)
	if token_info and check_token_validity(token_info):
		return redirect('generate_response')
	sp_oauth = SpotifyOAuth(
		client_id=settings.SPOTIFY_CLIENT_ID,
		client_secret=settings.SPOTIFY_CLIENT_SECRET,
		redirect_uri=settings.SPOTIFY_REDIRECT_URI,
		scope=settings.SPOTIFY_SCOPE,
		show_dialog=True,
	)
	auth_url = sp_oauth.get_authorize_url()
	return redirect(auth_url)

def callback(request):
	sp_oauth = SpotifyOAuth(
		client_id=settings.SPOTIFY_CLIENT_ID,
		client_secret=settings.SPOTIFY_CLIENT_SECRET,
		redirect_uri=settings.SPOTIFY_REDIRECT_URI,
		scope=settings.SPOTIFY_SCOPE,
	)

	code = request.GET.get('code')
	if not code:
		return redirect('login_page')  # User cancelled authorization

	try:
		token_info = sp_oauth.get_access_token(code)
	except spotipy.oauth2.SpotifyOauthError:
		return redirect('login_page')  # Authorization failed

	request.session['token_info'] = token_info
	return redirect('generate_response')    

def logout(request):
	if 'token_info' in request.session:
		del request.session['token_info']
	return redirect('login_page')
	
def retrieve_and_save_artist_tracks(artist_id, access_token):
	headers = {
		'Authorization': f'Bearer {access_token}'
	}

	try:
		artist = Artist.objects.get(id=artist_id)  # Fetch the Artist instance
	except Artist.DoesNotExist:
		logging.error(f"Artist with id {artist_id} does not exist in the database.")
		return []

	# Fetch artist's albums and singles
	albums_response = requests.get(
		f'https://api.spotify.com/v1/artists/{artist_id}/albums',
		headers=headers,
		params={'include_groups': 'album,single', 'limit': 50}
	)
	albums = albums_response.json()['items']

	new_tracks = []
	track_uris = []
	for album in albums:
		album_tracks_response = requests.get(
			f'https://api.spotify.com/v1/albums/{album["id"]}/tracks',
			headers=headers
		)
		album_tracks = album_tracks_response.json()['items']
		for track in album_tracks:
			if not Track.objects.filter(id=track['id']).exists():
				# Create and save new track
				new_track = Track(
					id=track['id'],
					name=track['name'],
					artist_id=artist,  # Assign the Artist instance
				)
				new_tracks.append(new_track)
				track_uris.append(track['uri'])

	# Bulk create new tracks in the database
	if new_tracks:
		with transaction.atomic():
			Track.objects.bulk_create(new_tracks)

	return track_uris

def get_closest_artist(artists, given_name, name_weight=0.7, popularity_weight=0.3):
	artist_names = [artist.name for artist in artists]
	
	vectorizer = TfidfVectorizer().fit_transform(artist_names + [given_name])
	vectors = vectorizer.toarray()
	
	cosine_similarities = cosine_similarity(vectors[-1:], vectors[:-1])[0]
	
	max_popularity = max(artist.popularity for artist in artists)
	normalized_popularities = [artist.popularity / max_popularity for artist in artists]
	
	final_scores = [
		(name_weight * cosine_similarity) + (popularity_weight * popularity)
		for cosine_similarity, popularity in zip(cosine_similarities, normalized_popularities)
	]

	best_artist_index = int(np.argmax(final_scores))
	return artists[best_artist_index]

def get_artist_ids(artist_names):
	artist_ids = []
	if artist_names:
		for name in artist_names:
			artists = Artist.objects.filter(name__icontains=name)
			if artists.exists():
				closest_artist = get_closest_artist(artists, name)
				if closest_artist:
					artist_ids.append(closest_artist.id)
	return artist_ids

def get_recommendations(sp, access_token, query):
	try:
		query = json.loads(query)
		seed_genres = query.get('genres', None)
		artists = query.get('artists', None)
		limit = min(query['playlist_size'], 100) if query.get('playlist_size', None) else 20
		recommended_tracks = []

		if seed_genres:
			headers = {
				'Authorization': f'Bearer {access_token}'
			}
			genre_recs_response = requests.get(
				'https://api.spotify.com/v1/recommendations',
				headers=headers,
				params={
					'seed_genres': ','.join(seed_genres),
					'limit': limit
				}
			)
			genre_recs = genre_recs_response.json()['tracks']
			genre_recs = [track['uri'] for track in genre_recs]
			recommended_tracks.extend(genre_recs)

		artist_ids = get_artist_ids(artists)
		for artist_id in artist_ids:
			tracks = list(Track.objects.filter(artist_id=artist_id))
			num_tracks = int(limit / len(artist_ids))
			if not tracks:
				logging.info(f'No tracks saved in database for artist {artist_id}')
				track_uris = retrieve_and_save_artist_tracks(artist_id, access_token)
				if len(track_uris) > num_tracks:
					track_uris = random.sample(track_uris, num_tracks)
				recommended_tracks.extend(track_uris)
			else:
				
				if len(tracks) > num_tracks:
					tracks = random.sample(tracks, num_tracks)
				recommended_tracks.extend([f'spotify:track:{track.id}' for track in tracks])

		# Ensure there are no duplicate track URIs
		recommended_tracks = list(set(recommended_tracks))

		random.shuffle(recommended_tracks)
		return recommended_tracks
	except Exception as e:
		raise Exception(f"Error getting recommendations: {str(e)}")


def create_playlist(sp, access_token, track_ids, playlist_name, retries=3):
	logging.info('Creating playlist...')
	try:
		uid = sp.me()['id']
		playlist_create_url = f"https://api.spotify.com/v1/users/{uid}/playlists"
		headers = {
			"Authorization": f"Bearer {access_token}",
			"Content-Type": "application/json"
		}
		playlist_create_data = {
			"name": playlist_name,  # Ensure the name is not too long
			"public": False
		}

		# Attempt to create the playlist with retries
		for attempt in range(retries):
			logging.info(f"Request URL: {playlist_create_url}")
			logging.info(f"Request Headers: {headers}")
			logging.info(f"Request Body: {json.dumps(playlist_create_data)}")
			playlist_create_response = requests.post(playlist_create_url, headers=headers, data=json.dumps(playlist_create_data), timeout=10, verify=False)
			logging.info(f"Response Status Code: {playlist_create_response.status_code}")
			logging.info(f"Response Headers: {playlist_create_response.headers}")
			logging.info(f"Response Body: {playlist_create_response.text}")

			if playlist_create_response.status_code == 201:
				break
			else:
				logging.error(f"Attempt {attempt + 1} failed to create playlist: {playlist_create_response.status_code}")
				logging.error(f"Response: {playlist_create_response.text}")
				time.sleep(2 ** attempt)  # Exponential backoff

		if playlist_create_response.status_code != 201:
			raise Exception(f"Failed to create playlist after {retries} attempts. Status code: {playlist_create_response.status_code}")

		playlist_response = playlist_create_response.json()
		playlist_id = playlist_response['id']

		# Chunk the track IDs into batches of 100
		track_chunks = [track_ids[i:i + 100] for i in range(0, len(track_ids), 100)]
		add_tracks_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"

		# Attempt to add tracks to the playlist in chunks
		for chunk in track_chunks:
			tracks_data = {
				"uris": chunk,  # Ensure track IDs are in the format 'spotify:track:<track_id>'
				"position": 0
			}
			for attempt in range(retries):
				logging.info(f"Request URL: {add_tracks_url}")
				logging.info(f"Request Headers: {headers}")
				logging.info(f"Request Body: {json.dumps(tracks_data)}")
				add_tracks_response = requests.post(add_tracks_url, headers=headers, data=json.dumps(tracks_data), timeout=10, verify=False)
				logging.info(f"Response Status Code: {add_tracks_response.status_code}")
				logging.info(f"Response Headers: {add_tracks_response.headers}")
				logging.info(f"Response Body: {add_tracks_response.text}")

				if add_tracks_response.status_code == 201:
					break
				else:
					logging.error(f"Attempt {attempt + 1} failed to add tracks: {add_tracks_response.status_code}")
					logging.error(f"Response: {add_tracks_response.text}")
					time.sleep(2 ** attempt)  # Exponential backoff

			if add_tracks_response.status_code != 201:
				raise Exception(f"Failed to add tracks after {retries} attempts. Status code: {add_tracks_response.status_code}")

		logging.info(f"Playlist {playlist_name} created successfully with ID: {playlist_id}")
		return playlist_response['external_urls']['spotify']

	except Exception as e:
		logging.error(f"Error creating playlist: {str(e)}")
		raise

def user_initial_prompt(request):
	try:
		artist_names = open('artist_names.txt', 'r').read()
		genre_names = open('genre_names.txt', 'r').read()
		script = '''I'm trying to generate a suitable Spotify playlist from the following user's prompt. 
					Please respond with only a response of the following format and nothing else: 
					{
						\"artists\" : None,
						\"genres\" : None,
						\"playlist_size\" : None
					}
					, replacing \"None\'s\" with specific value(s) based on the user prompt if sufficient information is given and excluding attributes with \'None\' value. (Composers count as artists too!)
					'''
		script_cont = f'''
					Regarding genres, please ONLY choose from this list (case-sensitive): {genre_names}.
					Please limit the maximum number of genres to 5.
					If specific artist values are given, don't include the genres that they belong to.
					If no genres are given, choose some that fit with the user's prompt.
					Regarding artists, please ONLY choose from this list (case-sensitive): {artist_names}.
					If the prompt is empty, respond with random genres.
					If a playlist size is not given, come up with a reasonable number based on the number of artists/genres given (increasing playlist size when there's a higher number), otherwise defaulting to 20.
					Here is the prompt: '''
		script = script + script_cont
		prompt = request.POST.get('prompt', '')
		completion = model.generate_content(f'{script} {prompt}')
		response = completion.text

		name_script = '''Based on the following user prompt and the genres/artists given, generate and give a creative and appropriate name for a Spotify playlist (and nothing else):'''
		name_completion = model.generate_content(f'{name_script} User prompt: {prompt} Playlist info: {response}')
		playlist_name = name_completion.text.strip().replace('"', '')
		return response, playlist_name
	except Exception as e:
		raise Exception(f"Error generating playlist info: {str(e)}")

def generate_response(request):
	token_info = request.session.get('token_info', None)
	if token_info is None or not check_token_validity(token_info):
		return redirect('login')

	access_token = token_info['access_token']
	sp = get_spotify_client(request)
	if not sp:
		return redirect('login_page')

	if request.method == 'POST':
		try:
			response, playlist_name = user_initial_prompt(request)
			print(response)
			recommendations = get_recommendations(sp, access_token, response)
			playlist_url = create_playlist(sp, access_token, recommendations, playlist_name[:100])  # Ensuring the playlist name is not too long
			return render(request, 'playlist/success.html', {
				'playlist_url': playlist_url
			})
		except Exception as e:
			return render(request, 'playlist/error.html', {
				'error_message': str(e)
			})

	return render(request, 'playlist/generate_response.html')
