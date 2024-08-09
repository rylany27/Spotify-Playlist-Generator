import os
import json
import time
import httpx
import asyncio
import requests
import multiprocessing
from dotenv import load_dotenv
from concurrent.futures import ProcessPoolExecutor, as_completed

# Load environment variables
load_dotenv()

class ArtistScraper:
    def __init__(self, parse_type="short", update=False, scrape_num=0,
                 num_users=3, workers=multiprocessing.cpu_count()):
        self.parse_type = parse_type
        self.update = update
        self.scrape_num = scrape_num
        self.num_users = num_users
        self.workers = workers

        self.SP_DCS = [os.getenv("SP_DC_" + str(i)) for i in range(num_users)]
        self.SP_KEYS = [os.getenv("SP_KEY_" + str(i)) for i in range(num_users)]

        self._setup_folders(parse_type)
        self.initialize_new_user()
        self.client = None
        self.tracks_data = []

    async def get_artist_albums(self, artist_id):
        albums = []
        query = f"https://api.spotify.com/v1/artists/{artist_id}/albums?include_groups=album,single&limit=50"
        result_json = await self.get_url_result_json(query)
        if result_json:
            albums.extend(result_json["items"])
            print(f"Retrieved {len(result_json['items'])} albums for artist {artist_id}")

        while result_json and result_json["next"]:
            result_json = await self.get_url_result_json(result_json["next"])
            if result_json:
                albums.extend(result_json["items"])
                print(f"Retrieved {len(result_json['items'])} more albums for artist {artist_id}")

        print(f"Total albums retrieved for artist {artist_id}: {len(albums)}")
        return albums

    async def get_album_tracks(self, album_id, artist_id, processed_track_ids):
        tracks = []
        query = f"https://api.spotify.com/v1/albums/{album_id}/tracks?limit=50"
        result_json = await self.get_url_result_json(query)
        if result_json:
            tracks.extend(result_json["items"])
            print(f"Retrieved {len(result_json['items'])} tracks for album {album_id}")

        while result_json and result_json["next"]:
            result_json = await self.get_url_result_json(result_json["next"])
            if result_json:
                tracks.extend(result_json["items"])
                print(f"Retrieved {len(result_json['items'])} more tracks for album {album_id}")

        print(f"Total tracks retrieved for album {album_id}: {len(tracks)}")
        self.save_tracks(tracks, artist_id, processed_track_ids)

    async def get_url_result_json(self, query_url, retries=5, backoff_factor=1):
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"Bearer {self.token}"}
            for attempt in range(retries):
                try:
                    response = await client.get(query_url, headers=headers)
                    response.raise_for_status()  # Raise an error for bad status codes
                    return response.json()
                except httpx.HTTPStatusError as e:
                    if response.status_code == 401:
                        print("Access token expired. Refreshing token...")
                        self.refresh_token()
                        headers["Authorization"] = f"Bearer {self.token}"  # Update headers with new token
                    elif response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 1))
                        print(f"Rate limit exceeded. Retrying after {retry_after} seconds.")
                        await asyncio.sleep(retry_after)
                        switch_server_and_user(self)
                        headers["Authorization"] = f"Bearer {self.token}"  # Update headers with new token
                    else:
                        print(f"HTTP error {response.status_code}: {response.text}")
                        break
                except httpx.RequestError as e:
                    print(f"An error occurred while requesting {query_url}: {str(e)}")
                    if attempt < retries - 1:
                        sleep_time = backoff_factor * (2 ** attempt)
                        print(f"Retrying in {sleep_time} seconds...")
                        await asyncio.sleep(sleep_time)
                    else:
                        print("Max retries exceeded.")
                        break
            return None

    def save_tracks(self, tracks, artist_id, processed_track_ids):
        for track in tracks:
            if track["id"] not in processed_track_ids:
                track_info = {
                    "model": "playlist.track",
                    "pk": track["id"],
                    "fields": {
                        "artist": artist_id,
                        "id": track["id"],
                        "name": track["name"]
                    }
                }
                self.tracks_data.append(track_info)
                processed_track_ids.add(track["id"])
        print(f"Saved {len(tracks)} unique tracks for artist {artist_id}")

    async def process_artist(self, artist_id):
        print(f"Processing artist {artist_id}")
        processed_track_ids = set()  # Set to keep track of unique track IDs for the artist
        albums = await self.get_artist_albums(artist_id)
        for album in albums:
            await self.get_album_tracks(album["id"], artist_id, processed_track_ids)

    async def start_async(self, artist_ids):
        async with httpx.AsyncClient() as client:
            self.client = client
            tasks = [self.process_artist(artist_id) for artist_id in artist_ids]
            await asyncio.gather(*tasks)

    def initialize_new_user(self):
        self._user = UserClient(sp_dc=self.SP_DCS[self.scrape_num % self.num_users],
                                sp_key=self.SP_KEYS[self.scrape_num % self.num_users])
        self.token = self._user._access_token

    def _setup_folders(self, parse_type):
        self.results_dir = parse_type + "_results/"
        if not os.path.isdir(self.results_dir):
            os.mkdir(self.results_dir)

    def save_to_json(self, batch_num):
        file_path = f'{self.results_dir}tracks_batch_{batch_num}.json'
        print(f"Saving data to {file_path}")
        with open(file_path, 'w') as outfile:
            json.dump(self.tracks_data, outfile, indent=4)
        print(f"Saved all track data to {file_path}")
        self.tracks_data = []  # Clear the list after saving

    def refresh_token(self):
        # Implement token refresh logic here
        self._user.refresh_token()
        self.token = self._user._access_token

class UserClient:
    def __init__(self, sp_dc=None, sp_key=None):
        self.sp_dc = os.getenv("SP_DC_0") if sp_dc is None else sp_dc
        self.sp_key = os.getenv("SP_KEY_0") if sp_key is None else sp_key

        self._verify_ssl = True
        self.__USER_AGENT = 'Mozilla/5.0'
        self.__HEADERS = {
                'User-Agent': self.__USER_AGENT,
                'Accept': 'application/json',
                'Origin': 'https://open.spotify.com',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
                'Referer': 'https://open.spotify.com/',
                'Te': 'trailers',
                'App-Platform': 'WebPlayer'
            }
        self.initialize_tokens()
        print("Successfully initialized user connection!")

    async def async_get(self, query_url, async_client):
        return await async_client.get(query_url, headers=self.__HEADERS)

    def initialize_tokens(self):
        with requests.session() as session:
            session.headers = self.__HEADERS
            cookies = {"sp_dc": self.sp_dc, "sp_key": self.sp_key}
            try:
                result_json = session.get(
                        'https://open.spotify.com/get_access_token',
                         verify=self._verify_ssl, cookies=cookies).json()
                self.is_anonymous = result_json['isAnonymous']
            except Exception as ex:
                print('An error occurred when generating an access token!', ex,
                      "\n This probably means the account was deleted.")
                exit(0)
            self._access_token = result_json['accessToken']
            self._client_id = result_json['clientId'] \
                    if result_json['clientId'].lower() != 'unknown' \
                    else self._client_id

            if 'client_token' in session.headers:
                session.headers.pop('client_token')
            if 'Authorization' in session.headers:
                session.headers.pop('Authorization')
            data = {
                "client_data": {
                    "client_version": "1.2.13.477.ga4363038",
                    "client_id": self._client_id,
                    "js_sdk_data":
                    {
                        "device_brand": "",
                        "device_id": "",
                        "device_model": "",
                        "device_type": "",
                        "os": "",
                        "os_version": ""
                    }
                }
            }
            response_json = session.post(
            'https://clienttoken.spotify.com/v1/clienttoken',
            json=data, verify=self._verify_ssl).json()
            self._client_token = response_json['granted_token']['token']

        self.__HEADERS.update({
                                'Client-Token': self._client_token,
                                'Authorization': f'Bearer {self._access_token}'
                                })

    def refresh_token(self):
        with requests.session() as session:
            session.headers = self.__HEADERS
            cookies = {"sp_dc": self.sp_dc, "sp_key": self.sp_key}
            try:
                result_json = session.get(
                        'https://open.spotify.com/get_access_token',
                         verify=self._verify_ssl, cookies=cookies).json()
                self.is_anonymous = result_json['isAnonymous']
            except Exception as ex:
                print('An error occurred when refreshing the access token!', ex)
                exit(0)
            self._access_token = result_json['accessToken']
            self.__HEADERS.update({
                'Authorization': f'Bearer {self._access_token}'
            })
            print("Access token refreshed successfully.")

def switch_server_and_user(scraper):
    try:
        print("Switching VPN & users")
        os.system("'/mnt/c/Program Files/NordVPN/nordvpn.exe' -c")
        fails = 0
        while not check_connection():
            time.sleep(1)
            fails += 1
            if fails > 12:
                os.system("'/mnt/c/Program Files/NordVPN/nordvpn.exe' -c")
                fails = 0
        print("New VPN connection established")
        scraper.scrape_num += 1
        scraper.initialize_new_user()
        print('Connected as new user')
    except Exception as e:
        print(f'*** Failure switching server and/or user: {e}')

def check_connection():
    try:
        requests.get('http://www.google.com', timeout=1)
        return True
    except:
        return False

def wait_for_rate_limit(result):
    wait_time = int(result.headers.get("Retry-After"))
    print(f"Rate limit exceeded. Please wait {wait_time} seconds.")
    time.sleep(wait_time)

def start_batch(args):
    artist_ids, scraper, batch_num = args
    try:
        asyncio.run(scraper.start_async(artist_ids))
        scraper.save_to_json(batch_num)
    except Exception as e:
        print(f"Error processing batch {batch_num}: {e}")
        switch_server_and_user(scraper)
        try:
            asyncio.run(scraper.start_async(artist_ids))
            scraper.save_to_json(batch_num)
        except Exception as e:
            print(f"Error reprocessing batch {batch_num}: {e}")

def read_artist_ids(file_path):
    with open(file_path, 'r') as file:
        artist_ids = [line.strip() for line in file.readlines()]
    return artist_ids

if __name__ == "__main__":
    import sys

    parse_type = "quick" if len(sys.argv) > 1 and (sys.argv[1] == "quick" or sys.argv[1] == "short") else "long"
    update = True if len(sys.argv) > 2 and sys.argv[2] == "True" else False
    scrape_num = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 0
    num_users = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].isdigit() else 3
    artist_file = sys.argv[5] if len(sys.argv) > 5 else "artist_ids.txt"

    if len(sys.argv) > 6 and sys.argv[6].isdigit():
        scraper = ArtistScraper(parse_type, update, scrape_num, num_users, int(sys.argv[6]))
    else:
        scraper = ArtistScraper(parse_type, update, scrape_num, num_users)

    artist_ids = read_artist_ids(artist_file)
    batches = [(artist_ids[i:i + 10], scraper, i // 10 + 1) for i in range(9000, len(artist_ids), 10)]

    with ProcessPoolExecutor(max_workers=scraper.workers) as executor:
        futures = [executor.submit(start_batch, batch) for batch in batches]
        for future in as_completed(futures):
            if future.exception() is not None:
                print(f"Error: {future.exception()}")
            else:
                print("A processor's execution finished successfully")
