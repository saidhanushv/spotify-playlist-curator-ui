import os
import uuid
from flask import Flask, render_template, request, redirect, session, url_for, flash, jsonify
from flask_session import Session  # Use server-side sessions
import requests
from bs4 import BeautifulSoup
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
import logging
from urllib.parse import urlencode

# Load environment variables (optional, for FLASK_SECRET_KEY)
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
# IMPORTANT: Set a strong secret key for session security
# You can generate one using: python -c 'import os; print(os.urandom(24))'
# Store it securely, e.g., in environment variables
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'fallback_super_secret_key_change_me')

# Configure server-side session
app.config['SESSION_TYPE'] = 'filesystem' # Store session files on the server's filesystem
app.config['SESSION_PERMANENT'] = False # Session expires when browser closes
app.config['SESSION_USE_SIGNER'] = True # Encrypt session cookie
# Consider SESSION_FILE_THRESHOLD, SESSION_FILE_DIR if needed
Session(app)

# --- Spotify Configuration ---
# These will be provided by the user in the form, but we need placeholders
# The Redirect URI *must* match the one set in your Spotify Developer Dashboard
SPOTIPY_REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI', 'http://127.0.0.1:5000/callback') # Use environment variable with fallback
# Scopes define the permissions the app requests from the user
SCOPE = 'playlist-modify-public playlist-modify-private user-read-private'

# --- Helper Functions ---

def get_top_100_songs(year):
    """
    Scrapes Billboard Year-End Hot 100 chart for a given year.
    Returns a list of tuples: [(song_title, artist_name), ...]
    Returns None if scraping fails.
    """
    try:
        url = f"https://www.billboard.com/charts/year-end/{year}/hot-100-songs"
        headers = { # Add headers to mimic a browser request
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)

        soup = BeautifulSoup(response.text, 'html.parser')

        # --- !! IMPORTANT: HTML structure might change !! ---
        # This part needs careful inspection of the Billboard page source
        # Adjust selectors based on current structure (Inspect Element in browser)
        # Example selectors (these WILL likely need updating):
        song_elements = soup.select('div.o-chart-results-list-row-container') # Find rows

        if not song_elements:
             logging.warning(f"No song elements found on Billboard page for {year}. Structure might have changed.")
             # Try alternative selectors (examples, may not work)
             song_elements = soup.select('li.o-chart-results-list__item')
             if not song_elements:
                  logging.error(f"Could not find song list structure for year {year} on Billboard.")
                  return None

        songs = []
        for item in song_elements:
            try:
                # Find title (might be in h3 with specific ID or class)
                title_tag = item.select_one('h3[id*="title-of-a-story"]') # Look for h3 with 'title-of-a-story' in its id
                if not title_tag: continue # Skip if title not found

                title = title_tag.get_text(strip=True)

                # Find artist (might be in span sibling to the title's parent, or within the same container)
                artist_tag = title_tag.find_next_sibling('span') # Common pattern
                if not artist_tag:
                    # Try finding span within the row/item with a specific class if sibling fails
                     artist_tag = item.select_one('span.c-label.a-no-trucate') # Another possible selector

                if not artist_tag: continue # Skip if artist not found

                artist = artist_tag.get_text(strip=True)

                if title and artist:
                    songs.append((title, artist))
                else:
                     logging.warning(f"Skipping row due to missing title or artist: {item.get_text(strip=True)[:50]}...")


            except Exception as e:
                logging.warning(f"Error parsing a song item: {e}. Item: {item.get_text(strip=True)[:50]}...")
                continue # Skip this item and try the next

        logging.info(f"Successfully scraped {len(songs)} songs for {year}.")
        return songs[:100] # Return only the top 100 even if more were found

    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching Billboard page for {year}: {e}")
        return None
    except Exception as e:
        logging.error(f"Error parsing Billboard page for {year}: {e}")
        return None

def create_spotify_oauth():
    """Creates a SpotifyOAuth object using credentials stored in session."""
    client_id = session.get('spotify_client_id')
    client_secret = session.get('spotify_client_secret')

    if not client_id or not client_secret:
        return None

    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE,
        cache_path=None # Don't use file cache with sessions
        # Removed cache_path=session_cache_path() logic as we store token in Flask session
    )

def get_spotify_token():
    """Checks session for token info, attempts refresh if needed."""
    token_info = session.get('spotify_token_info', None)
    if not token_info:
        logging.info("No token info found in session.")
        return None

    # Check if token is expired and refresh if necessary
    sp_oauth = create_spotify_oauth()
    if not sp_oauth:
        logging.warning("Cannot refresh token: Spotify credentials not in session.")
        # Clear potentially invalid token? Or just let it fail later?
        session.pop('spotify_token_info', None)
        return None

    # Spotipy's is_token_expired and refresh_access_token handle the logic
    if sp_oauth.is_token_expired(token_info):
        logging.info("Spotify token expired, attempting refresh.")
        try:
            token_info = sp_oauth.refresh_access_token(token_info['refresh_token'])
            session['spotify_token_info'] = token_info # Store refreshed token
            logging.info("Spotify token refreshed successfully.")
        except Exception as e:
            logging.error(f"Error refreshing Spotify token: {e}")
            # Clear bad token info and credentials, force re-auth
            session.pop('spotify_token_info', None)
            session.pop('spotify_client_id', None)
            session.pop('spotify_client_secret', None)
            return None # Failed to refresh

    return token_info


# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
def index():
    """Handles the main form submission and initiates the process."""
    if request.method == 'POST':
        # Store user-provided details and credentials in session
        session['playlist_year'] = request.form.get('year')
        session['user_name'] = request.form.get('name') # Store even if not used directly
        session['user_age'] = request.form.get('age')   # Store even if not used directly
        session['spotify_client_id'] = request.form.get('client_id')
        session['spotify_client_secret'] = request.form.get('client_secret')

        # Validate year input
        year = session.get('playlist_year')
        try:
            year_int = int(year)
            # Add reasonable year range check if desired (e.g., 1950-2025)
            if not (1940 < year_int < 2030):
                 raise ValueError("Year out of reasonable range")
        except (ValueError, TypeError):
            flash('Please enter a valid year (e.g., 2023).', 'error')
            return redirect(url_for('index'))

        if not session.get('spotify_client_id') or not session.get('spotify_client_secret'):
             flash('Spotify Client ID and Client Secret are required.', 'error')
             return redirect(url_for('index'))


        # --- Initiate Spotify Auth ---
        sp_oauth = create_spotify_oauth()
        if not sp_oauth:
             # Should not happen if validation above passed, but as safety
             flash('Error initializing Spotify authentication.', 'error')
             return redirect(url_for('index'))

        # Generate a unique state variable for CSRF protection
        state = str(uuid.uuid4())
        session['spotify_auth_state'] = state

        auth_url = sp_oauth.get_authorize_url(state=state)
        logging.info(f"Redirecting user to Spotify for authorization: {auth_url}")
        return redirect(auth_url)

    # GET request: Render the main form
    # Clear previous attempt's data if user revisits form
    session.pop('playlist_year', None)
    # Keep client ID/secret maybe? Or clear them too? Let's clear for safety.
    # session.pop('spotify_client_id', None)
    # session.pop('spotify_client_secret', None)
    session.pop('spotify_auth_state', None)
    # Don't clear token_info here, maybe user wants to reuse existing auth

    return render_template('index.html')

@app.route('/callback')
def callback():
    """Handles the redirect back from Spotify after authorization."""
    logging.info("Received callback from Spotify.")

    # Verify state parameter for CSRF protection
    received_state = request.args.get('state')
    expected_state = session.pop('spotify_auth_state', None)

    if not expected_state or received_state != expected_state:
        logging.error(f"State mismatch. Expected: {expected_state}, Received: {received_state}")
        flash('Authentication failed (state mismatch). Please try again.', 'error')
        return redirect(url_for('index'))

    # Check for errors from Spotify
    error = request.args.get('error')
    if error:
        logging.error(f"Spotify authorization error: {error}")
        flash(f'Spotify authorization failed: {error}. Please ensure you granted permissions.', 'error')
        # Clear credentials as they might be wrong or user denied access
        session.pop('spotify_client_id', None)
        session.pop('spotify_client_secret', None)
        return redirect(url_for('index'))

    # Exchange authorization code for tokens
    code = request.args.get('code')
    sp_oauth = create_spotify_oauth()

    if not sp_oauth:
        flash('Session expired or invalid credentials. Please enter details again.', 'error')
        return redirect(url_for('index'))

    if not code:
        flash('Authorization code missing in callback. Please try again.', 'error')
        return redirect(url_for('index'))

    try:
        logging.info("Attempting to fetch Spotify token.")
        token_info = sp_oauth.get_access_token(code, check_cache=False) # Don't check file cache
        session['spotify_token_info'] = token_info # Store token info in session
        session.modified = True  # Ensure session is saved
        logging.info("Successfully obtained and stored Spotify token.")
        
        # Get the year from the session and store it with the correct key
        year = session.get('playlist_year')
        if year:
            session['year'] = year  # Store with the key expected by other routes
            session.modified = True  # Ensure session is saved
        
        # Redirect to the generating page instead of directly to create_playlist
        return redirect(url_for('generating', year=year))
    except Exception as e:
        logging.error(f"Error getting Spotify access token: {e}")
        flash('Failed to get Spotify access token. Check credentials and try again.', 'error')
        # Clear potentially invalid credentials
        session.pop('spotify_client_id', None)
        session.pop('spotify_client_secret', None)
        return redirect(url_for('index'))

@app.route('/generating/<year>')
def generating(year):
    """Displays the generating page with real-time updates."""
    return render_template('generating.html', year=year)

@app.route('/create_playlist', methods=['GET'])
def create_playlist():
    """Create a Spotify playlist with the top 100 songs from the specified year."""
    try:
        # Get the year and Spotify token from the session
        year = session.get('year')
        token_info = session.get('spotify_token_info')
        
        # Log the session variables for debugging
        print(f"Create playlist - Session variables - year: {year}, token_info: {token_info is not None}")
        
        if not year:
            # Try to get the year from the URL parameter as a fallback
            year = request.args.get('year')
            if year:
                session['year'] = year
                session.modified = True
                print(f"Using year from URL parameter: {year}")
            else:
                print("Year not found in session or URL parameters")
                return jsonify({'error': 'Year not found. Please try again.'}), 400
        
        if not token_info:
            print("Spotify token not found in session")
            return jsonify({'error': 'Spotify token not found. Please authenticate again.'}), 400
        
        # Get the top 100 songs from the Billboard chart for the specified year
        songs = get_top_100_songs(year)
        if not songs:
            print(f"No songs found for year {year}")
            return jsonify({'error': f'No songs found for the year {year}'}), 400
        
        # Create a Spotify client
        sp = spotipy.Spotify(auth=token_info['access_token'])
        
        # Get the user ID
        user_info = sp.current_user()
        user_id = user_info['id']
        
        # Create a new playlist
        playlist_name = f"Billboard Top 100 - {year}"
        playlist_description = f"A playlist of the top 100 songs from Billboard's Hot 100 chart in {year}."
        print(f"Creating playlist: {playlist_name}")
        playlist = sp.user_playlist_create(user=user_id, name=playlist_name, public=False, description=playlist_description)
        print(f"Playlist created with ID: {playlist['id']}")
        
        # Initialize lists to track results
        added_tracks = []
        not_found_songs = []
        error_adding = False
        
        # Add songs to the playlist
        for title, artist in songs:
            try:
                # Search for the song on Spotify
                query = f"{title} artist:{artist}"
                results = sp.search(q=query, type='track', limit=1)
                
                # Check if the song was found
                if results['tracks']['items']:
                    track_uri = results['tracks']['items'][0]['uri']
                    
                    # Add the track to the playlist
                    sp.playlist_add_items(playlist_id=playlist['id'], items=[track_uri])
                    added_tracks.append(f"{title} by {artist}")
                    
                    # Log the success
                    print(f"Added to playlist: {title} by {artist}")
                else:
                    # Log the not found song
                    print(f"Not found on Spotify: {title} by {artist}")
                    not_found_songs.append(f"{title} by {artist}")
                    
            except Exception as e:
                # Log the error
                print(f"Error adding {title} by {artist} to playlist: {str(e)}")
                not_found_songs.append(f"{title} by {artist}")
                error_adding = True
        
        # Prepare the response
        if added_tracks:
            message = f"Successfully created playlist '{playlist_name}' with {len(added_tracks)} songs."
            if not_found_songs:
                message += f" {len(not_found_songs)} songs could not be found on Spotify."
        else:
            message = "Failed to add any songs to the playlist."
            error_adding = True
        
        # Prepare the response data
        response_data = {
            'success': len(added_tracks) > 0,
            'message': message,
            'playlist_url': playlist['external_urls']['spotify'],
            'playlist_name': playlist_name,
            'not_found': not_found_songs,
            'error_adding': error_adding
        }
        
        print(f"Sending response: {response_data}")
        
        # Return the response
        return jsonify(response_data)
        
    except Exception as e:
        # Log the error and return an error response
        print(f"Error in create_playlist: {str(e)}")
        import traceback
        traceback.print_exc()  # Print the full traceback for debugging
        return jsonify({'error': str(e)}), 500

@app.route('/search_songs', methods=['GET'])
def search_songs():
    """Search for songs on Spotify and return results in real-time."""
    try:
        # Get the year and Spotify token from the session
        year = session.get('year')
        token_info = session.get('spotify_token_info')
        
        # Log the session variables for debugging
        print(f"Session variables - year: {year}, token_info: {token_info is not None}")
        
        if not year:
            # Try to get the year from the URL parameter as a fallback
            year = request.args.get('year')
            if year:
                session['year'] = year
                session.modified = True
                print(f"Using year from URL parameter: {year}")
            else:
                print("Year not found in session or URL parameters")
                return jsonify({'error': 'Year not found. Please try again.'}), 400
        
        if not token_info:
            print("Spotify token not found in session")
            return jsonify({'error': 'Spotify token not found. Please authenticate again.'}), 400
        
        # Get the top 100 songs from the Billboard chart for the specified year
        songs = get_top_100_songs(year)
        if not songs:
            print(f"No songs found for year {year}")
            return jsonify({'error': f'No songs found for the year {year}'}), 400
        
        # Create a Spotify client
        sp = spotipy.Spotify(auth=token_info['access_token'])
        
        # Search for each song on Spotify
        song_results = []
        for index, (title, artist) in enumerate(songs):
            try:
                # Search for the song on Spotify
                query = f"{title} artist:{artist}"
                results = sp.search(q=query, type='track', limit=1)
                
                # Check if the song was found
                found = len(results['tracks']['items']) > 0
                
                # Add the result to the list
                song_results.append({
                    'index': index,
                    'title': title,
                    'artist': artist,
                    'found': found,
                    'error': None
                })
                
                # Log the search result
                print(f"Song {index + 1}/100: {title} by {artist} - {'Found' if found else 'Not Found'}")
                
            except Exception as e:
                # Log the error and add it to the results
                print(f"Error searching for {title} by {artist}: {str(e)}")
                song_results.append({
                    'index': index,
                    'title': title,
                    'artist': artist,
                    'found': False,
                    'error': str(e)
                })
        
        # Return the results
        return jsonify({
            'success': True,
            'songs': song_results
        })
        
    except Exception as e:
        # Log the error and return an error response
        print(f"Error in search_songs: {str(e)}")
        return jsonify({'error': str(e)}), 500

# --- Main Execution ---
if __name__ == '__main__':
    # Use 0.0.0.0 to make it accessible on your network, default port is 5000
    # Use debug=True only for development, REMOVE for production
    app.run(host='127.0.0.1', port=5000, debug=True)