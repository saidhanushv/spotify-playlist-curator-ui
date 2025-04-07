# Spotify Playlist Generator

A web application that generates Spotify playlists based on Billboard's Top 100 songs from any year.

## Features

- Scrapes Billboard's Year-End Hot 100 chart for any year
- Creates a Spotify playlist with the found songs
- Real-time progress tracking during playlist creation
- Handles songs that can't be found on Spotify

## Local Development

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Create a `.env` file with your `FLASK_SECRET_KEY` (Optional)
4. Run the app: `python app.py`

## Spotify Developer Setup

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/)
2. Create a new application
3. Copy the Client ID and Client Secret to use in the app

## License

MIT