import os
import time
import random
import threading
from datetime import timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_session import Session
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyException

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'une_cle_secrete_super_securisee_elotify')

# --- CONFIGURATION SESSION CÔTÉ SERVEUR ---
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
Session(app)

# Variable globale pour l'inactivité
LAST_ACTIVITY_TIME = time.time()

# --- BASE DE DONNÉES NEON POSTGRESQL ---
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    """Initialise la table dans Neon si elle n'existe pas encore."""
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tracks_scores (
                playlist_id VARCHAR(255),
                track_id VARCHAR(255),
                name TEXT,
                artist TEXT,
                image_url TEXT,
                elo INT,
                PRIMARY KEY (playlist_id, track_id)
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ Erreur initialisation BDD Neon : {e}")

# Initialisation BDD au démarrage
init_db()

# --- CONFIGURATION SPOTIFY OAUTH ---
SPOTIPY_CLIENT_ID = os.environ.get('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.environ.get('SPOTIPY_CLIENT_SECRET')
SPOTIPY_REDIRECT_URI = os.environ.get('SPOTIPY_REDIRECT_URI', 'https://elotify.onrender.com/callback')
SCOPE = 'playlist-read-private playlist-read-collaborative user-read-playback-state user-modify-playback-state'

def get_spotify_oauth():
    return SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE,
        cache_handler=spotipy.cache_handler.FlaskSessionCacheHandler(session)
    )

def get_spotify_client():
    sp_oauth = get_spotify_oauth()
    token_info = sp_oauth.validate_token(sp_oauth.get_cached_token())
    if not token_info:
        return None
    return spotipy.Spotify(auth=token_info['access_token'])

def get_user_profile_cached(sp):
    """Récupère le profil et le met en cache individuellement dans la session de l'utilisateur."""
    now = time.time()
    cached_profile = session.get('user_profile')
    cached_time = session.get('user_profile_timestamp', 0)
    
    if cached_profile and (now - cached_time < 600):
        return cached_profile
    
    try:
        user_info = sp.current_user()
        images = user_info.get('images', [])
        image_url = images[0]['url'] if images else None
        
        profile = {
            'display_name': user_info.get('display_name', 'Utilisateur'),
            'image': image_url
        }
        
        session['user_profile'] = profile
        session['user_profile_timestamp'] = now
        session.modified = True
        return profile
    except Exception as e:
        print(f"⚠️ Erreur récupération profil : {e}")
        return session.get('user_profile')

# --- GESTION DU SCORE ELO & BDD ---
def calculate_elo(elo_a, elo_b, outcome_a, k=32):
    expected_a = 1 / (1 + 10 ** ((elo_b - elo_a) / 400))
    expected_b = 1 - expected_a
    
    outcome_b = 1.0 - outcome_a
    
    new_elo_a = round(elo_a + k * (outcome_a - expected_a))
    new_elo_b = round(elo_b + k * (outcome_b - expected_b))
    
    return new_elo_a, new_elo_b

def load_local_scores():
    playlist_id = session.get('selected_playlist_id')
    if not playlist_id or not DATABASE_URL:
        return session.get('local_scores_fallback', {})

    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT track_id, name, artist, image_url, elo FROM tracks_scores WHERE playlist_id = %s;",
            (playlist_id,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        scores = {}
        for row in rows:
            scores[row['track_id']] = {
                'name': row['name'],
                'artist': row['artist'],
                'image_url': row['image_url'],
                'elo': row['elo']
            }
        return scores
    except Exception as e:
        print(f"⚠️ Erreur chargement BDD Neon : {e}")
        return session.get('local_scores_fallback', {})

def _save_to_db_async(playlist_id, scores_to_update):
    """Effectue l'écriture dans Neon de façon asynchrone (en arrière-plan)."""
    if not DATABASE_URL or not playlist_id:
        return

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        for track_id, data in scores_to_update.items():
            cur.execute("""
                INSERT INTO tracks_scores (playlist_id, track_id, name, artist, image_url, elo)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (playlist_id, track_id) 
                DO UPDATE SET elo = EXCLUDED.elo, name = EXCLUDED.name, artist = EXCLUDED.artist, image_url = EXCLUDED.image_url;
            """, (
                playlist_id,
                track_id,
                data.get('name', ''),
                data.get('artist', ''),
                data.get('image_url', ''),
                data.get('elo', 1000)
            ))

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ Erreur sauvegarde asynchrone BDD Neon : {e}")

def save_local_scores(scores_to_update):
    playlist_id = session.get('selected_playlist_id')
    if not playlist_id:
        return

    if not DATABASE_URL:
        fallback = session.get('local_scores_fallback', {})
        fallback.update(scores_to_update)
        session['local_scores_fallback'] = fallback
        session.modified = True
        return

    # Exécution dans un thread séparé pour supprimer toute latence lors du vote
    thread = threading.Thread(target=_save_to_db_async, args=(playlist_id, scores_to_update))
    thread.daemon = True
    thread.start()

# --- ROUTES AUTHENTIFICATION ---
@app.route('/login')
def login():
    sp_oauth = get_spotify_oauth()
    auth_url = sp_oauth.get_authorize_url()
    return redirect(auth_url)

@app.route('/callback')
def callback():
    sp_oauth = get_spotify_oauth()
    
    saved_playlist_id = session.get('selected_playlist_id')
    session.clear()
    
    if saved_playlist_id:
        session['selected_playlist_id'] = saved_playlist_id
        session.permanent = True
        
    code = request.args.get('code')
    token_info = sp_oauth.get_access_token(code)
    
    if token_info:
        if saved_playlist_id:
            return redirect(url_for('index'))
        return redirect(url_for('liste_playlists'))
        
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- ROUTE SELECTION PLAYLIST ---
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
            
            total = 0
            if 'items' in pl and isinstance(pl['items'], dict):
                total = pl['items'].get('total', 0)
            elif 'tracks' in pl and isinstance(pl['tracks'], dict):
                total = pl['tracks'].get('total', 0)
                    
            playlists.append({
                "id": pl.get('id'),
                "name": pl.get('name', 'Playlist sans nom'),
                "images": pl.get('images', []),
                "total_tracks": total
            })
            
        return render_template('playlists.html', playlists=playlists, user=get_user_profile_cached(sp))
    except Exception as e:
        print(f"⚠️ Erreur liste_playlists : {e}")
        return redirect(url_for('login'))

@app.route('/select_playlist/<playlist_id>')
def select_playlist(playlist_id):
    session.permanent = True
    session['selected_playlist_id'] = playlist_id
    session.pop('tracks_cache', None)
    # Écrase l'entrée dans l'historique de navigation du navigateur
    return '<script>window.location.replace("/");</script>'

# --- ROUTE PRINCIPALE (DUEL) ---
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

    if 'tracks_cache' not in session or not session['tracks_cache']:
        try:
            raw_items = []
            offset = 0
            limit = 100

            # Pagination complète pour charger l'intégralité des titres
            while True:
                results = sp.playlist_tracks(playlist_id, limit=limit, offset=offset)
                items = results.get('items', []) if isinstance(results, dict) else []
                
                if not items:
                    break
                    
                raw_items.extend(items)
                
                if len(items) < limit:
                    break
                    
                offset += limit

            tracks = []
            for el in raw_items:
                if not el:
                    continue
                
                track = el.get('item') or el.get('track')
                
                if isinstance(track, dict) and track.get('id'):
                    album = track.get('album', {})
                    images = album.get('images', []) if isinstance(album, dict) else []
                    image_url = images[0]['url'] if images else ''
                    
                    artists = track.get('artists', [])
                    artist_name = ", ".join([a.get('name', '') for a in artists if isinstance(a, dict)])
                    
                    tracks.append({
                        'id': track['id'],
                        'name': track.get('name', 'Titre inconnu'),
                        'artist': artist_name or 'Artiste inconnu',
                        'uri': track.get('uri', ''),
                        'image_url': image_url
                    })
                    
            session['tracks_cache'] = tracks
        except Exception as e:
            print(f"⚠️ Erreur chargement titres playlist : {e}")
            return redirect(url_for('liste_playlists'))

    tracks = session.get('tracks_cache', [])
    if len(tracks) < 2:
        return f"La playlist sélectionnée ne contient pas assez de morceaux valides (morceaux trouvés: {len(tracks)}) pour réaliser un match.", 400

    track_a, track_b = random.sample(tracks, 2)

    elo_a = scores.get(track_a['id'], {}).get('elo', 1000)
    elo_b = scores.get(track_b['id'], {}).get('elo', 1000)

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

# --- ROUTE VOTE ---
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

    elo_a = scores.get(id_a, {}).get('elo', 1000)
    elo_b = scores.get(id_b, {}).get('elo', 1000)

    new_elo_a, new_elo_b = calculate_elo(elo_a, elo_b, outcome)

    delta_a = new_elo_a - elo_a
    delta_b = new_elo_b - elo_b

    updated_scores = {
        id_a: {
            'name': track_a['name'],
            'artist': track_a['artist'],
            'image_url': track_a['image_url'],
            'elo': new_elo_a
        },
        id_b: {
            'name': track_b['name'],
            'artist': track_b['artist'],
            'image_url': track_b['image_url'],
            'elo': new_elo_b
        }
    }

    # Sauvegarde asynchrone (instantané pour l'utilisateur)
    save_local_scores(updated_scores)

    # Mise à jour immédiate du cache de session
    for t in session.get('tracks_cache', []):
        if t['id'] == id_a:
            t['elo'] = new_elo_a
        elif t['id'] == id_b:
            t['elo'] = new_elo_b
    session.modified = True

    sign_a = f"+{delta_a}" if delta_a > 0 else f"{delta_a}"
    sign_b = f"+{delta_b}" if delta_b > 0 else f"{delta_b}"

    if outcome == 1.0:
        session['dernier_resultat'] = f"Victoire de : {track_a['name']} ({sign_a} pts) contre {track_b['name']} ({sign_b} pts) !"
    elif outcome == 0.0:
        session['dernier_resultat'] = f"Victoire de : {track_b['name']} ({sign_b} pts) contre {track_a['name']} ({sign_a} pts) !"
    else:
        session['dernier_resultat'] = f"Match nul entre {track_a['name']} ({sign_a} pts) et {track_b['name']} ({sign_b} pts) !"

    return redirect(url_for('index'))

# --- CONTROLES LECTURE SPOTIFY ---
@app.route('/listen/<path:track_uri>', methods=['POST'])
def listen(track_uri):
    sp = get_spotify_client()
    if not sp:
        return jsonify({"error": "Non authentifié"}), 401
    try:
        sp.start_playback(uris=[track_uri])
        return jsonify({"status": "playing", "uri": track_uri})
    except SpotifyException as e:
        if e.http_status == 404:
            print("⚠️ Aucun lecteur Spotify actif trouvé.")
            return jsonify({"warning": "Ouvre Spotify sur ton appareil pour écouter l'extrait."}), 200
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/toggle-pause', methods=['POST'])
def toggle_pause():
    sp = get_spotify_client()
    if not sp:
        return jsonify({"error": "Non authentifié"}), 401
    try:
        playback = sp.current_playback()
        if playback and playback.get('is_playing'):
            sp.pause_playback()
            return jsonify({"status": "paused"})
        else:
            sp.start_playback()
            return jsonify({"status": "playing"})
    except SpotifyException as e:
        if e.http_status == 404:
            print("⚠️ Aucun lecteur Spotify actif trouvé.")
            return jsonify({"warning": "Ouvre Spotify sur ton appareil pour contrôler la lecture."}), 200
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- ROUTE CLASSEMENT & API TOP 5 ---
@app.route('/classement')
def classement():
    scores = load_local_scores()
    tracks = list(scores.values())
    tracks.sort(key=lambda x: x.get('elo', 1000), reverse=True)
    sp = get_spotify_client()
    user = get_user_profile_cached(sp) if sp else None
    return render_template('classement.html', tracks=tracks, user=user)

@app.route('/api/top5')
def api_top5():
    scores = load_local_scores()
    tracks = list(scores.values())
    tracks.sort(key=lambda x: x.get('elo', 1000), reverse=True)
    top5 = tracks[:5]
    return jsonify(top5)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)