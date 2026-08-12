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
from collections import Counter

app = Flask(__name__)

# Clé secrète fixe
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'une_cle_secrete_super_securisee_elotify_12345')

# Configuration Session ultra-rapide (Mémoire/Disque local avec expiration 30 jours)
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
Session(app)

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id VARCHAR(255) PRIMARY KEY,
                active_playlist_id VARCHAR(255),
                theme VARCHAR(50) DEFAULT 'green'
            );
        """)
        cur.execute("""
            ALTER TABLE user_preferences 
            ADD COLUMN IF NOT EXISTS theme VARCHAR(50) DEFAULT 'green';
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ Erreur BDD : {e}")

init_db()

SPOTIPY_CLIENT_ID = os.environ.get('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.environ.get('SPOTIPY_CLIENT_SECRET')
SPOTIPY_REDIRECT_URI = os.environ.get('SPOTIPY_REDIRECT_URI', 'https://elotify.onrender.com/callback')
SCOPE = 'playlist-read-private playlist-read-collaborative user-read-playback-state user-modify-playback-state'

def get_spotify_oauth(show_dialog=False):
    return SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE,
        show_dialog=show_dialog,
        cache_handler=spotipy.cache_handler.FlaskSessionCacheHandler(session)
    )

def get_spotify_client():
    sp_oauth = get_spotify_oauth()
    token_info = sp_oauth.validate_token(sp_oauth.get_cached_token())
    if not token_info:
        return None
    return spotipy.Spotify(auth=token_info['access_token'])

def get_user_profile_cached(sp):
    now = time.time()
    cached_profile = session.get('user_profile')
    cached_time = session.get('user_profile_timestamp', 0)
    
    if cached_profile and (now - cached_time < 3600): # Cache 1h
        return cached_profile
    
    try:
        user_info = sp.current_user()
        images = user_info.get('images', [])
        profile = {
            'id': user_info.get('id'),
            'display_name': user_info.get('display_name', 'Utilisateur'),
            'image': images[0]['url'] if images else None
        }
        session['user_profile'] = profile
        session['user_profile_timestamp'] = now
        session.modified = True
        return profile
    except Exception:
        return session.get('user_profile')

def load_local_scores():
    # Priorité absolue au cache session pour éliminer la latence
    if 'scores_cache' in session and session['scores_cache']:
        return session['scores_cache']

    playlist_id = session.get('selected_playlist_id')
    if not playlist_id or not DATABASE_URL:
        return {}

    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT track_id, name, artist, image_url, elo FROM tracks_scores WHERE playlist_id = %s;", (playlist_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        scores = {row['track_id']: dict(row) for row in rows}
        session['scores_cache'] = scores
        session.modified = True
        return scores
    except Exception as e:
        print(f"⚠️ Erreur chargement BDD : {e}")
        return {}

def _save_to_db_async(playlist_id, scores_to_update):
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
            """, (playlist_id, track_id, data.get('name', ''), data.get('artist', ''), data.get('image_url', ''), data.get('elo', 1000)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ Erreur écriture arrière-plan : {e}")

def save_local_scores(scores_to_update):
    # 1. Mise à jour instantanée du cache local en session
    scores = session.get('scores_cache', {})
    scores.update(scores_to_update)
    session['scores_cache'] = scores
    session.modified = True

    # 2. Sauvegarde PostgreSQL déportée dans un thread séparé (non-bloquant)
    playlist_id = session.get('selected_playlist_id')
    if DATABASE_URL and playlist_id:
        threading.Thread(target=_save_to_db_async, args=(playlist_id, scores_to_update), daemon=True).start()

def calculate_elo(elo_a, elo_b, outcome_a, k=32):
    expected_a = 1 / (1 + 10 ** ((elo_b - elo_a) / 400))
    expected_b = 1 - expected_a
    new_elo_a = round(elo_a + k * (outcome_a - expected_a))
    new_elo_b = round(elo_b + k * ((1.0 - outcome_a) - expected_b))
    return new_elo_a, new_elo_b

@app.route('/login')
def login():
    return render_template('login.html', auth_url=get_spotify_oauth(show_dialog=True).get_authorize_url())

@app.route('/callback')
def callback():
    sp_oauth = get_spotify_oauth()
    token_info = sp_oauth.get_access_token(request.args.get('code'))
    if token_info:
        session.permanent = True
        return redirect(url_for('index'))
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/playlists')
def liste_playlists():
    sp = get_spotify_client()
    if not sp: return redirect(url_for('login'))
    try:
        results = sp.current_user_playlists(limit=50)
        playlists = []
        for pl in results.get('items', []):
            if not pl: continue
            total = pl.get('tracks', {}).get('total', 0) if isinstance(pl.get('tracks'), dict) else 0
            images = pl.get('images', [])
            playlists.append({"id": pl.get('id'), "name": pl.get('name', 'Sans nom'), "image_url": images[0]['url'] if images else None, "tracks_count": total})
        return render_template('playlists.html', playlists=playlists, user=get_user_profile_cached(sp))
    except Exception:
        return redirect(url_for('login'))

@app.route('/select_playlist/<playlist_id>')
def select_playlist(playlist_id):
    session['selected_playlist_id'] = playlist_id
    session.pop('tracks_cache', None)
    session.pop('scores_cache', None)
    return '<script>window.location.replace("/");</script>'

@app.route('/')
def index():
    sp = get_spotify_client()
    if not sp: return redirect(url_for('login'))
    profile = get_user_profile_cached(sp)

    playlist_id = session.get('selected_playlist_id')
    if not playlist_id: return redirect(url_for('liste_playlists'))

    scores = load_local_scores()

    if 'tracks_cache' not in session or not session['tracks_cache']:
        try:
            raw_items, offset = [], 0
            while True:
                res = sp.playlist_tracks(playlist_id, limit=100, offset=offset)
                items = res.get('items', []) if isinstance(res, dict) else []
                if not items: break
                raw_items.extend(items)
                if len(items) < 100: break
                offset += 100

            tracks = []
            for el in raw_items:
                track = el.get('item') or el.get('track') if el else None
                if isinstance(track, dict) and track.get('id'):
                    images = track.get('album', {}).get('images', [])
                    artists = ", ".join([a.get('name', '') for a in track.get('artists', []) if isinstance(a, dict)])
                    tracks.append({'id': track['id'], 'name': track.get('name', 'Titre inconnu'), 'artist': artists or 'Inconnu', 'uri': track.get('uri', ''), 'image_url': images[0]['url'] if images else ''})
            session['tracks_cache'] = tracks
        except Exception:
            return redirect(url_for('liste_playlists'))

    tracks = session.get('tracks_cache', [])
    if len(tracks) < 2: return "Playlist trop courte (minimum 2 titres).", 400

    track_a, track_b = random.sample(tracks, 2)
    track_a['elo'] = scores.get(track_a['id'], {}).get('elo', 1000)
    track_b['elo'] = scores.get(track_b['id'], {}).get('elo', 1000)

    dernier_resultat = session.pop('dernier_resultat', None)
    return render_template('index.html', track_a=track_a, track_b=track_b, dernier_resultat=dernier_resultat, user=profile, current_theme=session.get('theme', 'green'))

@app.route('/vote', methods=['POST'])
def vote():
    id_a = request.form.get('id_a')
    id_b = request.form.get('id_b')
    outcome = float(request.form.get('outcome', 0.5))

    tracks = session.get('tracks_cache', [])
    track_a = next((t for t in tracks if t['id'] == id_a), None)
    track_b = next((t for t in tracks if t['id'] == id_b), None)

    if track_a and track_b:
        scores = load_local_scores()
        elo_a = scores.get(id_a, {}).get('elo', 1000)
        elo_b = scores.get(id_b, {}).get('elo', 1000)

        new_elo_a, new_elo_b = calculate_elo(elo_a, elo_b, outcome)
        delta_a, delta_b = new_elo_a - elo_a, new_elo_b - elo_b

        updated = {
            id_a: {'name': track_a['name'], 'artist': track_a['artist'], 'image_url': track_a['image_url'], 'elo': new_elo_a},
            id_b: {'name': track_b['name'], 'artist': track_b['artist'], 'image_url': track_b['image_url'], 'elo': new_elo_b}
        }
        save_local_scores(updated)

        sign_a = f"+{delta_a}" if delta_a > 0 else f"{delta_a}"
        sign_b = f"+{delta_b}" if delta_b > 0 else f"{delta_b}"
        if outcome == 1.0: session['dernier_resultat'] = f"🏆 Victoire de {track_a['name']} ({sign_a} Elo) face à {track_b['name']} ({sign_b} Elo)"
        elif outcome == 0.0: session['dernier_resultat'] = f"🏆 Victoire de {track_b['name']} ({sign_b} Elo) face à {track_a['name']} ({sign_a} Elo)"
        else: session['dernier_resultat'] = f"🤝 Match nul entre {track_a['name']} ({sign_a} Elo) et {track_b['name']} ({sign_b} Elo)"

    return redirect(url_for('index'))

@app.route('/listen/<path:track_uri>', methods=['POST'])
def listen(track_uri):
    sp = get_spotify_client()
    if not sp: return jsonify({"error": "Non authentifié"}), 401
    try:
        sp.start_playback(uris=[track_uri])
        return jsonify({"status": "playing"})
    except Exception as e:
        return jsonify({"warning": "Ouvre Spotify sur ton appareil."}), 200

@app.route('/toggle-pause', methods=['POST'])
def toggle_pause():
    sp = get_spotify_client()
    if not sp: return jsonify({"error": "Non authentifié"}), 401
    try:
        playback = sp.current_playback()
        if playback and playback.get('is_playing'):
            sp.pause_playback()
            return jsonify({"status": "paused"})
        sp.start_playback()
        return jsonify({"status": "playing"})
    except Exception:
        return jsonify({"warning": "Erreur lecteur Spotify."}), 200

@app.route('/seek_offset/<offset_seconds>', methods=['POST'])
def seek_offset(offset_seconds):
    sp = get_spotify_client()
    if not sp: return jsonify({"error": "Non authentifié"}), 401
    try:
        playback = sp.current_playback()
        if playback and playback.get('is_playing') and playback.get('progress_ms') is not None:
            target_ms = max(0, playback['progress_ms'] + (int(offset_seconds) * 1000))
            sp.seek_track(position_ms=target_ms)
            return jsonify({'status': 'success'})
        return jsonify({'warning': 'Lance Spotify.'})
    except Exception:
        return jsonify({'warning': 'Erreur lecteur Spotify.'})

@app.route('/set_theme/<theme_name>', methods=['POST'])
def set_theme_route(theme_name):
    session['theme'] = theme_name
    return jsonify({"status": "success"})

@app.route('/classement')
def classement():
    scores = load_local_scores()
    tracks = list(scores.values())
    tracks.sort(key=lambda x: x.get('elo', 1000), reverse=True)
    sp = get_spotify_client()
    return render_template('classement.html', ranking=tracks, user=get_user_profile_cached(sp) if sp else None)

@app.route('/stats')
def stats():
    sp = get_spotify_client()
    if not sp: return redirect(url_for('login'))
    tracks = session.get('tracks_cache', [])
    artist_counter = Counter()
    for t in tracks:
        for a in t.get('artist', '').split(','):
            if a.strip(): artist_counter[a.strip()] += 1
    return render_template('stats.html', sorted_artists=artist_counter.most_common(), total_tracks=len(tracks), user=get_user_profile_cached(sp))

@app.route('/quit')
def quit_app():
    sp = get_spotify_client()
    if sp:
        try: sp.pause_playback()
        except Exception: pass
    scores = load_local_scores()
    tracks = list(scores.values())
    tracks.sort(key=lambda x: x.get('elo', 1000), reverse=True)
    session.pop('tracks_cache', None)
    return render_template('quit.html', top5=tracks[:5])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)