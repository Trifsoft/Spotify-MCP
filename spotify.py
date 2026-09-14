from mcp.server.mcpserver import MCPServer
import os
import json
import time
import requests
import webbrowser
import base64
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = os.environ.get(
    "SPOTIFY_SCOPE",
    "ugc-image-upload user-read-playback-state user-modify-playback-state user-read-currently-playing app-remote-control streaming playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public user-follow-modify user-follow-read user-read-playback-position user-top-read user-read-recently-played user-library-modify user-library-read"
)
TOKEN_CACHE_PATH = os.path.expanduser("~/.spotify_mcp_token.json")

_token_cache = {"access_token": None, "expires_at": 0}
_auth_result = {"access_token": None, "expires_in": None}


def _auth_header():
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


def _save_refresh_token(refresh_token):
    with open(TOKEN_CACHE_PATH, "w") as f:
        json.dump({"refresh_token": refresh_token}, f)


def _load_refresh_token():
    if os.path.exists(TOKEN_CACHE_PATH):
        with open(TOKEN_CACHE_PATH) as f:
            return json.load(f).get("refresh_token")
    return None


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        code = query.get("code", [None])[0]

        if code:
            # Exchange the code for tokens right here, and cache the refresh_token
            resp = requests.post("https://accounts.spotify.com/api/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI
            }, headers=_auth_header())
            resp.raise_for_status()
            data = resp.json()

            _save_refresh_token(data["refresh_token"])
            _auth_result["access_token"] = data["access_token"]
            _auth_result["expires_in"] = data.get("expires_in", 3600)

            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>Authorized! You can close this tab.</body></html>")
        else:
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>No code received.</body></html>")

    def log_message(self, format, *args):
        pass  # silence default request logging


def _authorize_via_browser():
    """
    Opens the browser for the user to authorize the app. The local server
    captures the redirect, exchanges the code for tokens, and writes the
    refresh_token to disk itself. Returns the fresh access_token.
    """
    server = HTTPServer(("127.0.0.1", 8888), _CallbackHandler)

    auth_url = "https://accounts.spotify.com/authorize?" + urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE
    })
    webbrowser.open(auth_url)

    server.handle_request()  # blocks until the callback hits, then stops
    server.server_close()

    return _auth_result["access_token"], _auth_result["expires_in"]


def _request_token_via_refresh(refresh_token):
    """Uses a saved refresh_token to get a new access_token, no browser needed."""
    resp = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }, headers=_auth_header())
    resp.raise_for_status()
    return resp.json()


def get_access_token():
    """
    Returns a valid access token, refreshing or re-authorizing only when needed.
    Caches in memory for the process lifetime; caches refresh_token on disk.
    """
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    refresh_token = _load_refresh_token()
    if refresh_token:
        data = _request_token_via_refresh(refresh_token)
        access_token, expires_in = data["access_token"], data.get("expires_in", 3600)
    else:
        access_token, expires_in = _authorize_via_browser()

    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = time.time() + expires_in - 60
    return access_token


def make_call(method, endpoint, return_response = True, **kwargs):
    url = f"https://api.spotify.com/v1{endpoint}"
    headers = {"Authorization": f"Bearer {get_access_token()}"}

    resp = requests.request(method, url, headers=headers, **kwargs)
    resp.raise_for_status()

    if return_response and resp.content:
        return resp.json()
    return None

def make_action(method, endpoint, **kwargs) -> bool:
    try:
        make_call(method, endpoint, return_response=False, **kwargs)
        return True
    except:
        return False


# ----------- MCP ------------

mcp = MCPServer("Demo")

@mcp.tool()
def getCurrentSong():
    """
    Fetch the track currently playing on the user's Spotify account.

    Returns a dict like {"name": ..., "artists": ...} if something is playing,
    or None if nothing is playing.
    """
    resp = make_call("GET", "/me/player/currently-playing")

    if resp is not None:
        track = resp["item"]
        return {
            "name": track["name"],
            "artists": ", ".join(a["name"] for a in track["artists"])
        }
    return None


@mcp.tool()
def pause():
    """
    Tries to pause playback. Returns a boolean whether or not it succeeded or not.
    """
    return make_action("PUT", "/me/player/pause")

@mcp.tool()
def search(search_query: str, comma_separated_search_types: str):
    """
    Make a search based on the search query and comma-separated types of values to return.
    Only valid types are: album, artist, playlist, track

    Returns a dict like:
    {
        "albums": [{
            "name": str,
            "artists": [{
                "name": str,
                "id": str
            }],
            "id": str
        }],
        "tracks": [{
            "name": str,
            "artists": [{
                "name": str,
                "id": str
            }],
            "id": str
        }], 
        "artists": [{
            "name": str,
            "id": str
        }], 
        "playlists": [{
            "id": str,
            "description": str,
            "name": str,
            "owner": {
                "id": str,
                "name" str
            }
        
        }]
    }
    """

    search_results = make_call("GET", "/search", params={"q": search_query, "type": comma_separated_search_types})
    res = {"albums": [], "tracks": [], "artists": [], "playlists": [], }
    if("albums" in search_results):
        for album in search_results["albums"]["items"]:
            artists = []
            for artist in album["artists"]:
                artists.append({
                    "id": artist["id"],
                    "name": artist["name"]
                })
            res["albums"].append({
                "name": album["name"],
                "artists": artists,
                "id": album["id"]
            })
    if("tracks" in search_results):
        for track in search_results["tracks"]["items"]:
            artists = []
            for artist in track["artists"]:
                artists.append({
                    "id": artist["id"],
                    "name": artist["name"]
                })
            res["tracks"].append({
                "name": track["name"],
                "artists": artists,
                "id": track["id"]
            })
    if("artists" in search_results):
        for artist in search_results["artists"]["items"]:
            res["artists"].append({
                "id": artist["id"],
                "name": artist["name"]
            })

    if("playlists" in search_results):
        for playlist in search_results["playlists"]["items"]:
            res["playlists"].append({
                "name": playlist["name"],
                "description": playlist["description"],
                "id": playlist["id"],
                "owner": {
                    "name": playlist["owner"]["display_name"],
                    "id": playlist["owner"]["id"],
                }
            })
    return res

@mcp.tool()
def availableDevices():
    """
    Returns a list of available devices as a list of dicts {"id": str, "name": str, "is_active": bool}
    """
    devices = make_call("GET", "/me/player/devices")
    return [{"id": device["id"], "name": device["name"], "is_active": device["is_active"]} for device in devices["devices"]]

@mcp.tool()
def playSong(song_id: str, device_id: str | None = None):
    """
    Plays a song based on the provided song_id on a device with its provided device_id. If no device id is provided, a default (active) device will be used.
    """
    params = {"device_id": device_id} if device_id is not None else None
    make_call("PUT", "/me/player/play", return_response=False, params = params, json={"uris": [f"spotify:track:{song_id}"]})

@mcp.tool()
def nextSong():
    """
    Plays next song in queue. Returns a boolean value of whether or not the action succeeded or not.
    """
    return make_action("POST", "/me/player/next", return_response=False)

@mcp.tool()
def previousSong():
    """
    Plays previous song in queue. Returns a boolean value of whether or not the action succeeded or not.
    """
    return make_action("POST", "/me/player/previous")

if __name__ == "__main__":
    import sys
    if "--mcp" in sys.argv:
        mcp.run()
    elif "--auth" in sys.argv:
        get_access_token()
        print("Authorization complete. Token cached — you can now run with --mcp.")
    elif "--toggle-play" in sys.argv:
        print(togglePlayback())
    elif "--search" in sys.argv:
        print(search("die for you", "track"))
    elif "--play-song" in sys.argv:
        playSong("2Ch7LmS7r2Gy2kc64wv3Bz", device_id="9baf2b182aa03c28a9498dc650e3d7a7cf4e8296")
    elif "--devices" in sys.argv:
        print(availableDevices())
    else:
        print(getCurrentSong())