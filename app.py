import os
import json
import random
import time
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Import conditionnel de psycopg2 pour la base Neon (Render)
try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'cle_secrete_par_defaut_dev')

# --- CONFIGURATION SPOTIFY ---
SPOTIPY_CLIENT_ID = os.environ.get('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.environ.get('SPOTIPY_CLIENT_SECRET')
SPOTIPY_REDIRECT_URI = os.environ.get('SPOTIPY_REDIRECT_URI', 'http://127.0.0.1:5000/callback')

# SCOPES : Ajout des droits pour les playlists privées et collaboratives (évite les 403)
SCOPE = (
    "playlist-read-private "
    "playlist-read-collaborative "
    "user-modify-playback-state "
    "user-read-playback-state"
)

DATABASE_URL = os.environ.get('DATABASE_URL')
LAST_ACTIVITY_TIME = time.time()

# --- GESTION AUTHENTIFICATION SPOTIFY ---
def create_spotify_oauth():
    return SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE,
        cache_handler=spotipy.cache_handler.FlaskSessionCacheHandler(session)
    )

def get_spotify_client():
    sp_oauth = create_spotify_oauth()
    token_info = sp_oauth.get_cached_token()
    if not token_info:
        return None
    if sp_oauth.is_token_expired(token_info):
        try:
            token_info = sp_oauth.refresh_access_token(token_info['refresh_token'])
        except Exception as e:
            print(f"⚠️ Erreur lors du rafraîchissement du token : {e}")
            return None
    return spotipy.Spotify(auth=token_info['access_token'])

def get_user_profile_cached(sp):
    if 'user_profile' not in session:
        try:
            session['user_profile'] = sp.current_user()
        except Exception:
            session['user_profile'] = {'display_name': 'Mélomane'}
    return session['user_profile']

# --- GESTION DES SCORES (POSTGRESQL / JSON LOCAL) ---
def load_local_scores(target_playlist_id=None):
    playlist_id = target_playlist_id or session.get('selected_playlist_id', 'generique')
    
    # 1. Mode Base de données (Neon)
    if DATABASE_URL and HAS_PSYCOPG2:
        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("SELECT scores_json FROM playlist_scores WHERE playlist_id = %s;", (playlist_id,))
            row = cur.fetchone()
            cur.close()
            if row:
                return json.loads(row[0])
        except Exception as e:
            print(f"⚠️ Erreur lecture DB : {e}")
        finally:
            if conn:
                conn.close()

    # 2. Mode Fichier JSON (Local)
    filename = f"classement_{playlist_id}.json"
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Erreur lecture fichier JSON local : {e}")
            
    return {}

def save_local_scores(scores):
    playlist_id = session.get('selected_playlist_id', 'generique')
    
    # 1. Mode Base de données (Neon)
    if DATABASE_URL and HAS_PSYCOPG2:
        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS playlist_scores (
                    playlist_id VARCHAR(255) PRIMARY KEY,
                    scores_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            json_data = json.dumps(scores, ensure_ascii=False)
            cur.execute("""
                INSERT INTO playlist_scores (playlist_id, scores_json, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (playlist_id) 
                DO UPDATE SET scores_json = EXCLUDED.scores_json, updated_at = CURRENT_TIMESTAMP;
            """, (playlist_id, json_data))
            conn.commit()
            cur.close()
        except Exception as e:
            print(f"⚠️ Erreur sauvegarde DB : {e}")
        finally:
            if conn:
                conn.close()

    # 2. Mode Fichier JSON (Local)
    filename = f"classement_{playlist_id}.json"
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(scores, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"⚠️ Erreur sauvegarde JSON local : {e}")

# --- CALCUL DE L'ELO ---
def calculate_elo(rating_a, rating_b, outcome_a, k=32):
    expected_a = 1 / (1 + 10 ** ((rating_b - rating_a) / 400))
    expected_b = 1 - expected_a
    outcome_b = 1.0 - outcome_a
    new_rating_a = round(rating_a + k * (outcome_a - expected_a))
    new_rating_b = round(rating_b + k * (outcome_b - expected_b))
    return new_rating_a, new_rating_b


# --- ROUTES DE L'APPLICATION ---

@app.route('/')
def index():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for('login'))

    playlist_id = session.get('selected_playlist_id')
    if not playlist_id:
        return redirect(url_for('liste_playlists'))

    scores = load_local_scores()

    # Si la session ou la DB ne contient pas la liste des titres
    if 'tracks_cache' not in session or not session['tracks_cache']:
        try:
            results = sp.playlist_tracks(playlist_id)
            items = results.get('items', [])
            tracks = []
            
            for item in items:
                track = item.get('track')
                if track and track.get('id'):
                    image_url = track['album']['images'][0]['url'] if track['album']['images'] else ''
                    artist_name = ", ".join([a['name'] for a in track['artists']])
                    
                    tracks.append({
                        'id': track['id'],
                        'name': track['name'],
                        'artist': artist_name,
                        'uri': track['uri'],
                        'image_url': image_url
                    })
                    
            session['tracks_cache'] = tracks
        except Exception as e:
            print(f"⚠️ Erreur chargement titres : {e}")
            return redirect(url_for('liste_playlists'))

    tracks = session.get('tracks_cache', [])
    if len(tracks) < 2:
        return "La playlist sélectionnée ne contient pas assez de morceaux pour réaliser un match.", 400

    # Sélection aléatoire de 2 titres différents
    track_a, track_b = random.sample(tracks, 2)

    # Récupération de l'Elo (par défaut 1500)
    elo_a = scores.get(track_a['id'], {}).get('elo', 1500)
    elo_b = scores.get(track_b['id'], {}).get('elo', 1500)

    track_a['elo'] = elo_a
    track_b['elo'] = elo_b

    dernier_resultat = session.pop('dernier_resultat', None)

    return render_template(
        'index.html', 
        track_a=track_a, 
        track_b=track_b, 
        dernier_resultat=dernier_resultat,
        user=get_user_profile_cached(sp)
    )

@app.route('/vote', methods=['POST'])
def vote():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    id_a = request.form.get('id_a')
    id_b = request.form.get('id_b')
    outcome = float(request.form.get('outcome', 0.5))

    tracks = session.get('tracks_cache', [])
    track_a = next((t for t in tracks if t['id'] == id_a), None)
    track_b = next((t for t in tracks if t['id'] == id_b), None)

    if not track_a or not track_b:
        return redirect(url_for('index'))

    scores = load_local_scores()

    elo_a = scores.get(id_a, {}).get('elo', 1500)
    elo_b = scores.get(id_b, {}).get('elo', 1500)

    new_elo_a, new_elo_b = calculate_elo(elo_a, elo_b, outcome)

    scores[id_a] = {
        'name': track_a['name'],
        'artist': track_a['artist'],
        'image_url': track_a['image_url'],
        'elo': new_elo_a
    }
    scores[id_b] = {
        'name': track_b['name'],
        'artist': track_b['artist'],
        'image_url': track_b['image_url'],
        'elo': new_elo_b
    }

    save_local_scores(scores)

    winner_name = track_a['name'] if outcome == 1.0 else track_b['name']
    session['dernier_resultat'] = f"Victoire de : {winner_name} !"

    return redirect(url_for('index'))

@app.route('/playlists')
def liste_playlists():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for('login'))
        
    try:
        results = sp.current_user_playlists(limit=50)
        raw_playlists = results.get('items', [])
        playlists = []
        
        for pl in raw_playlists:
            if not pl: 
                continue
            
            # Récupération sécurisée du nombre de morceaux (teste plusieurs emplacements possibles)
            tracks_info = pl.get('tracks', {})
            if isinstance(tracks_info, dict):
                total_tracks = tracks_info.get('total', 0)
            else:
                total_tracks = 0
            
            # Alternative si total était à 0
            if total_tracks == 0 and 'total_tracks' in pl:
                total_tracks = pl.get('total_tracks', 0)
                    
            playlists.append({
                "id": pl.get('id'),
                "name": pl.get('name', 'Playlist sans nom'),
                "images": pl.get('images', []),
                "total_tracks": total_tracks
            })
            
        return render_template('playlists.html', playlists=playlists, user=get_user_profile_cached(sp))
    except Exception as e:
        print(f"⚠️ Erreur liste_playlists : {e}")
        session.clear()
        return redirect(url_for('login'))

@app.route('/select_playlist/<playlist_id>')
def select_playlist(playlist_id):
    session['selected_playlist_id'] = playlist_id
    session.pop('tracks_cache', None) # Vider le cache de l'ancienne playlist
    return redirect(url_for('index'))

@app.route('/classement')
def classement():
    sp = get_spotify_client()
    scores = load_local_scores()
    
    # Tri des titres par score Elo décroissant
    sorted_tracks = sorted(scores.values(), key=lambda x: x['elo'], reverse=True)
    
    user_profile = get_user_profile_cached(sp) if sp else None
    return render_template('classement.html', tracks=sorted_tracks, user=user_profile)

@app.route('/listen/<path:track_uri>', methods=['POST'])
def listen(track_uri):
    sp = get_spotify_client()
    if sp:
        try:
            sp.start_playback(uris=[track_uri])
            return jsonify({'status': 'playing'})
        except Exception as e:
            print(f"⚠️ Erreur start_playback : {e}")
            return jsonify({'status': 'error', 'message': str(e)}), 400
    return jsonify({'status': 'not_authenticated'}), 401

@app.route('/toggle-pause', methods=['POST'])
def toggle_pause():
    sp = get_spotify_client()
    if sp:
        try:
            playback = sp.current_playback()
            if playback and playback.get('is_playing'):
                sp.pause_playback()
                return jsonify({'status': 'paused'})
            else:
                sp.start_playback()
                return jsonify({'status': 'playing'})
        except Exception as e:
            print(f"⚠️ Erreur toggle_pause : {e}")
            return jsonify({'status': 'error', 'message': str(e)}), 400
    return jsonify({'status': 'not_authenticated'}), 401

# --- AUTHENTIFICATION ---

@app.route('/login')
def login():
    sp_oauth = create_spotify_oauth()
    auth_url = sp_oauth.get_authorize_url()
    return redirect(auth_url)

@app.route('/callback')
def callback():
    sp_oauth = create_spotify_oauth()
    session.clear()
    code = request.args.get('code')
    token_info = sp_oauth.get_access_token(code)
    return redirect(url_for('liste_playlists'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)