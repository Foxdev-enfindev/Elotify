import random
import os
import json
import time
import threading
from flask import Flask, render_template, request, redirect, url_for, jsonify, session
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Tentative d'importation de psycopg2 pour Neon.tech (silencieux si absent en local)
try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

app = Flask(__name__)
# Clé de session Flask
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "elotify_kpop_local_secret_session_key")

# Configuration des accès API Spotify & Base de données
CLIENT_ID = os.environ.get("SPOTIPY_CLIENT_ID", "b969cdabdc8443afb3bc0f494f0513bc")
CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET", "6c3bc4b35b474028b605d9f358c230d4")
REDIRECT_URI = os.environ.get("SPOTIPY_REDIRECT_URI", "https://elotify.onrender.com/callback")
DATABASE_URL = os.environ.get("DATABASE_URL")

K_FACTOR = 32
DEFAULT_RATING = 1000
DERNIER_RESULTAT = ""
LAST_ACTIVITY_TIME = time.time()

# --- GESTION DE L'AUTHENTIFICATION SPOTIFY ---

def create_spotify_oauth():
    """Crée le gestionnaire OAuth Spotipy."""
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope="playlist-read-private playlist-read-collaborative user-modify-playback-state user-read-playback-state user-read-private",
        show_dialog=True  # Force Spotify à afficher l'écran d'autorisation / choix de compte
    )

def get_spotify_client():
    """Récupère l'instance Spotipy associée au token enregistre en session."""
    token_info = session.get('token_info', None)
    if not token_info:
        return None
    
    auth_manager = create_spotify_oauth()
    if auth_manager.is_token_expired(token_info):
        try:
            token_info = auth_manager.refresh_access_token(token_info['refresh_token'])
            session['token_info'] = token_info
        except Exception as e:
            print(f"⚠️ Erreur de rafraîchissement du token : {e}")
            session.clear()
            return None

    return spotipy.Spotify(auth=token_info['access_token'])


# --- GESTION DE LA BASE DE DONNÉES / SAUVEGARDES ---

def init_db():
    """Crée la table des scores dans Neon si connecté."""
    if not DATABASE_URL or not HAS_PSYCOPG2:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS playlist_scores (
                playlist_id TEXT PRIMARY KEY,
                scores_json TEXT NOT NULL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("⚡ Connexion à la base de données Neon réussie !")
    except Exception as e:
        print(f"⚠️ Erreur initialisation DB : {e}")

def load_local_scores():
    playlist_id = session.get('selected_playlist_id', 'generique')
    
    # 1. Mode Base de données (Render / Neon)
    if DATABASE_URL and HAS_PSYCOPG2:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("SELECT scores_json FROM playlist_scores WHERE playlist_id = %s;", (playlist_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return json.loads(row[0])
        except Exception as e:
            print(f"⚠️ Erreur lecture DB : {e}")

    # 2. Mode Fichier JSON (Local)
    filename = f"classement_{playlist_id}.json"
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_local_scores(tracks_data):
    playlist_id = session.get('selected_playlist_id', 'generique')
    
    # 1. Mode Base de données (Render / Neon)
    if DATABASE_URL and HAS_PSYCOPG2:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            scores_json = json.dumps(tracks_data, ensure_ascii=False)
            cur.execute("""
                INSERT INTO playlist_scores (playlist_id, scores_json)
                VALUES (%s, %s)
                ON CONFLICT (playlist_id) 
                DO UPDATE SET scores_json = EXCLUDED.scores_json;
            """, (playlist_id, scores_json))
            conn.commit()
            cur.close()
            conn.close()
            return
        except Exception as e:
            print(f"⚠️ Erreur écriture DB : {e}")

    # 2. Mode Fichier JSON (Local)
    filename = f"classement_{playlist_id}.json"
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(tracks_data, f, ensure_ascii=False, indent=4)

# Initialisation DB
init_db()


# --- LOGIQUE MÉTIER & CALCULS ELO ---

def fetch_and_cache_playlist(sp):
    playlist_id = session.get('selected_playlist_id')
    if not playlist_id:
        raise Exception("Aucune playlist sélectionnée.")

    print(f"🔄 Synchronisation de la playlist {playlist_id}...")
    local_scores = load_local_scores()
    tracks = {}
    offset = 0
    limit = 100
    while True:
        try:
            results = sp.playlist_tracks(playlist_id, limit=limit, offset=offset)
        except Exception as e:
            print(f"❌ Erreur API Spotify lors de la récupération des titres : {e}")
            if local_scores: return local_scores
            raise e
            
        if not results or 'items' not in results: break
        items = results['items']
        if len(items) == 0: break
        
        for item in items:
            if not item or 'item' not in item or not item['item']: continue
            track = item['item']
            track_id = track.get('id')
            if not track_id: continue
            
            current_elo = local_scores[track_id]['elo'] if track_id in local_scores else DEFAULT_RATING
            artists = track.get('artists')
            artist_name = artists[0].get('name', 'Artiste inconnu') if artists else 'Artiste inconnu'
            album = track.get('album', {})
            images = album.get('images', [])
            image_url = images[1]['url'] if len(images) > 1 else (images[0]['url'] if images else '')
            
            tracks[track_id] = {
                "id": track_id,
                "name": track.get('name', 'Titre inconnu'),
                "artist": artist_name,
                "uri": track.get('uri'),
                "image_url": image_url,
                "elo": current_elo
            }
            
        if not results.get('next'): break
        offset += limit
        
    if tracks: save_local_scores(tracks)
    return tracks

def get_updated_tracks(sp):
    local_scores = load_local_scores()
    if local_scores and len(local_scores) > 5: return local_scores
    return fetch_and_cache_playlist(sp)

def get_user_profile_cached(sp):
    user_profile = {"display_name": "Utilisateur", "image": ""}
    if not sp: return user_profile
    try:
        me = sp.current_user()
        user_profile["display_name"] = me.get("display_name", "Utilisateur")
        if me.get("images"): user_profile["image"] = me["images"][0]["url"]
    except Exception as e:
        print(f"⚠️ Profil indisponible : {e}")
    return user_profile

def update_elo(rating_a, rating_b, outcome_a):
    ea = 1 / (1 + 10 ** ((rating_b - rating_a) / 400))
    return round(rating_a + K_FACTOR * (outcome_a - ea)), round(rating_b + K_FACTOR * ((1 - outcome_a) - (1 - ea)))

def watch_heartbeat():
    global LAST_ACTIVITY_TIME
    if os.environ.get("RENDER"):
        return
    while True:
        time.sleep(1)
        if time.time() - LAST_ACTIVITY_TIME > 6:
            print("🔌 Fermeture automatique (Mode local)...")
            os._exit(0)

threading.Thread(target=watch_heartbeat, daemon=True).start()


# --- ROUTES D'AUTHENTIFICATION (SPOTIFY OAUTH) ---

@app.route('/login')
def login():
    auth_manager = create_spotify_oauth()
    auth_url = auth_manager.get_authorize_url()
    return redirect(auth_url)

@app.route('/callback')
def callback():
    """Récupère le code de réponse envoyé par Spotify après la connexion."""
    auth_manager = create_spotify_oauth()
    session.clear()
    
    code = request.args.get('code')
    if code:
        try:
            # On passe explicitement le code sans laisser Spotipy deviner l'URL de retour
            token_info = auth_manager.get_access_token(code, check_cache=False)
            session['token_info'] = token_info
            return redirect(url_for('liste_playlists'))
        except Exception as e:
            print(f"❌ Erreur lors de l'obtention du token : {e}")
            return f"Erreur de connexion : {e}"
            
    return "Code d'autorisation manquant."@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# --- ROUTES PRINCIPALES ---

@app.route('/')
def index():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for('login'))
        
    if 'selected_playlist_id' not in session:
        return redirect(url_for('liste_playlists'))
        
    global DERNIER_RESULTAT
    try:
        tracks = get_updated_tracks(sp)
    except Exception as e:
        print(f"⚠️ Erreur accès playlist : {e}")
        session.clear()
        return redirect(url_for('login'))

    track_ids = list(tracks.keys())
    if len(track_ids) < 2: return "La playlist ne contient pas assez de morceaux valides."
    id_a, id_b = random.sample(track_ids, 2)
    return render_template('index.html', track_a=tracks[id_a], track_b=tracks[id_b], dernier_resultat=DERNIER_RESULTAT, user=get_user_profile_cached(sp))

@app.route('/playlists')
def liste_playlists():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for('login'))
        
    try:
        results = sp.current_user_playlists()
        raw_playlists = results.get('items', [])
        playlists = []
        
        for pl in raw_playlists:
            if not pl: continue
            playlist_id = pl.get('id')
            
            total_tracks = 0
            try:
                tracks_meta = sp.playlist_tracks(playlist_id, limit=1)
                total_tracks = tracks_meta.get('total', 0)
            except:
                try: total_tracks = pl.get('tracks', {}).get('total', 0)
                except: total_tracks = 0
                
            top5 = []
            try:
                session_temp = session.get('selected_playlist_id')
                session['selected_playlist_id'] = playlist_id
                data = load_local_scores()
                if session_temp: session['selected_playlist_id'] = session_temp
                else: session.pop('selected_playlist_id', None)
                
                if data:
                    sorted_tracks = sorted(data.values(), key=lambda x: x['elo'], reverse=True)
                    top5 = sorted_tracks[:5]
            except: pass
                    
            playlists.append({
                "id": playlist_id,
                "name": pl.get('name', 'Playlist sans nom'),
                "images": pl.get('images', []),
                "total_tracks": total_tracks,
                "top5": top5
            })
            
        return render_template('playlists.html', playlists=playlists, user=get_user_profile_cached(sp))
    except Exception as e:
        print(f"⚠️ Erreur liste_playlists (403/Token) : {e}")
        session.clear()
        return redirect(url_for('login'))

@app.route('/select-playlist/<playlist_id>')
def select_playlist(playlist_id):
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for('login'))
        
    session['selected_playlist_id'] = playlist_id
    try: 
        fetch_and_cache_playlist(sp)
    except Exception as e:
        print(f"⚠️ Erreur lors de la sélection : {e}")
    return redirect(url_for('index'))

@app.route('/vote', methods=['POST'])
def vote():
    global DERNIER_RESULTAT, LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for('login'))
        
    id_a, id_b, outcome = request.form.get('id_a'), request.form.get('id_b'), float(request.form.get('outcome'))
    tracks = get_updated_tracks(sp)
    if id_a in tracks and id_b in tracks:
        track_a, track_b = tracks[id_a], tracks[id_b]
        old_elo_a, old_elo_b = track_a['elo'], track_b['elo']
        new_elo_a, new_elo_b = update_elo(old_elo_a, old_elo_b, outcome)
        tracks[id_a]['elo'], tracks[id_b]['elo'] = new_elo_a, new_elo_b
        save_local_scores(tracks)
        diff_a, diff_b = new_elo_a - old_elo_a, new_elo_b - old_elo_b
        sign_a = "+" if diff_a >= 0 else ""
        sign_b = "+" if diff_b >= 0 else ""
        if outcome == 1.0: DERNIER_RESULTAT = f"🏆 Victoire de {track_a['name']} ({sign_a}{diff_a} Elo) face à {track_b['name']} ({sign_b}{diff_b} Elo)"
        elif outcome == 0.0: DERNIER_RESULTAT = f"🏆 Victoire de {track_b['name']} ({sign_b}{diff_b} Elo) face à {track_a['name']} ({sign_a}{diff_a} Elo)"
        else: DERNIER_RESULTAT = f"🤝 Match nul entre {track_a['name']} ({sign_a}{diff_a}) et {track_b['name']} ({sign_b}{diff_b})"
    return redirect(url_for('index'))

@app.route('/classement')
def classement():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for('login'))
        
    tracks = get_updated_tracks(sp)
    sorted_tracks = sorted(tracks.values(), key=lambda x: x['elo'], reverse=True)
    return render_template('classement.html', tracks=sorted_tracks)

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    return '', 204

@app.route('/listen/<uri>', methods=['GET', 'POST'])
def listen(uri):
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    sp = get_spotify_client()
    if sp:
        try: sp.start_playback(uris=[uri])
        except: pass
    return '', 204

@app.route('/toggle-pause', methods=['POST'])
def toggle_pause():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    sp = get_spotify_client()
    if sp:
        try:
            playback = sp.current_playback()
            if playback and playback.get('is_playing'):
                sp.pause_playback()
                return jsonify({"status": "paused"})
            else:
                sp.start_playback()
                return jsonify({"status": "playing"})
        except: pass
    return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)