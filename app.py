import random
import os
import json
import time
import threading
from flask import Flask, render_template, request, redirect, url_for, jsonify, session
import spotipy
from spotipy.oauth2 import SpotifyOAuth

app = Flask(__name__)
# Clé requise pour activer le stockage des variables 'session' de Flask
app.secret_key = "elotify_kpop_local_secret_session_key"

CLIENT_ID = "b969cdabdc8443afb3bc0f494f0513bc"
CLIENT_SECRET = "6c3bc4b35b474028b605d9f358c230d4"
REDIRECT_URI = "http://127.0.0.1:8888/callback"

K_FACTOR = 32
DEFAULT_RATING = 1000
DERNIER_RESULTAT = ""

# Verrous d'activité temporelle pour l'exécution sous .vbs
LAST_ACTIVITY_TIME = time.time()
CACHED_USER_PROFILE = None

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI,
    scope="playlist-read-private playlist-read-collaborative user-modify-playback-state user-read-playback-state user-read-private"
))

def get_playlist_data_file():
    """Génère un nom de fichier JSON unique basé sur la playlist active en session."""
    playlist_id = session.get('selected_playlist_id')
    if playlist_id:
        return f"classement_{playlist_id}.json"
    return "classement_generique.json"

def load_local_scores():
    filename = get_playlist_data_file()
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f: return json.load(f)
    return {}

def save_local_scores(tracks_data):
    filename = get_playlist_data_file()
    with open(filename, 'w', encoding='utf-8') as f: json.dump(tracks_data, f, ensure_ascii=False, indent=4)

def fetch_and_cache_playlist():
    playlist_id = session.get('selected_playlist_id')
    if not playlist_id:
        raise Exception("Aucune playlist sélectionnée.")

    print(f"🔄 Tentative de synchronisation avec la playlist {playlist_id}...")
    local_scores = load_local_scores()
    tracks = {}
    offset = 0
    limit = 100
    while True:
        try: results = sp.playlist_items(playlist_id, limit=limit, offset=offset)
        except Exception as e:
            print(f"❌ Erreur API Spotify : {e}")
            if local_scores: return local_scores
            raise Exception("Impossible de contacter Spotify.")
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
            tracks[track_id] = {"id": track_id, "name": track.get('name', 'Titre inconnu'), "artist": artist_name, "uri": track.get('uri'), "image_url": image_url, "elo": current_elo}
        if not results.get('next'): break
        offset += limit
    if tracks: save_local_scores(tracks)
    return tracks

def get_updated_tracks():
    local_scores = load_local_scores()
    if local_scores and len(local_scores) > 5: return local_scores
    return fetch_and_cache_playlist()

def get_user_profile_cached():
    global CACHED_USER_PROFILE
    if CACHED_USER_PROFILE is not None: return CACHED_USER_PROFILE
    user_profile = {"display_name": "Utilisateur", "image": ""}
    try:
        print("👤 Récupération initiale du profil utilisateur...")
        me = sp.current_user()
        user_profile["display_name"] = me.get("display_name", "Utilisateur")
        if me.get("images"): user_profile["image"] = me["images"][0]["url"]
        CACHED_USER_PROFILE = user_profile
    except Exception as e:
        print(f"⚠️ Impossible de récupérer le profil : {e}")
        return user_profile
    return CACHED_USER_PROFILE

def update_elo(rating_a, rating_b, outcome_a):
    ea = 1 / (1 + 10 ** ((rating_b - rating_a) / 400))
    return round(rating_a + K_FACTOR * (outcome_a - ea)), round(rating_b + K_FACTOR * ((1 - outcome_a) - (1 - ea)))

def watch_heartbeat():
    global LAST_ACTIVITY_TIME
    while True:
        time.sleep(1)
        if time.time() - LAST_ACTIVITY_TIME > 6:
            print("🔌 Aucun navigateur détecté depuis 6 secondes. Fermeture de sécurité...")
            try: sp.pause_playback()
            except: pass
            os._exit(0)

threading.Thread(target=watch_heartbeat, daemon=True).start()

@app.route('/')
def index():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    # Redirige vers la sélection si aucune playlist n'est en mémoire
    if 'selected_playlist_id' not in session:
        return redirect(url_for('liste_playlists'))
        
    global DERNIER_RESULTAT
    tracks = get_updated_tracks()
    track_ids = list(tracks.keys())
    if len(track_ids) < 2: return "La playlist ne contient pas assez de morceaux valides."
    id_a, id_b = random.sample(track_ids, 2)
    user_profile = get_user_profile_cached()
    return render_template('index.html', track_a=tracks[id_a], track_b=tracks[id_b], dernier_resultat=DERNIER_RESULTAT, user=user_profile)

@app.route('/playlists')
def liste_playlists():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    try:
        results = sp.current_user_playlists()
        raw_playlists = results.get('items', [])
        playlists = []
        
        for pl in raw_playlists:
            if not pl: continue
            playlist_id = pl.get('id')
            
            # Système de calcul triple sécurité pour obtenir le nombre réel de morceaux
            total_tracks = 0
            try:
                # Étape 1 : Appel léger et officiel avec limit=1 (valide pour l'API)
                tracks_meta = sp.playlist_items(playlist_id, limit=1)
                total_tracks = tracks_meta.get('total', 0)
            except:
                try:
                    # Étape 2 : Lecture directe dans les données de la liste en cas d'échec
                    total_tracks = pl.get('tracks', {}).get('total', 0)
                except:
                    total_tracks = 0
                
            # Récupération du Top 5 local s'il existe pour la bulle flottante
            top5 = []
            filename = f"classement_{playlist_id}.json"
            if os.path.exists(filename):
                try:
                    with open(filename, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        sorted_tracks = sorted(data.values(), key=lambda x: x['elo'], reverse=True)
                        top5 = sorted_tracks[:5]
                except:
                    pass
                    
            playlists.append({
                "id": playlist_id,
                "name": pl.get('name', 'Playlist sans nom'),
                "images": pl.get('images', []),
                "total_tracks": total_tracks,
                "top5": top5
            })
            
        return render_template('playlists.html', playlists=playlists, user=get_user_profile_cached())
    except Exception as e:
        return f"Erreur lors de la récupération des playlists : {e}"

@app.route('/select-playlist/<playlist_id>')
def select_playlist(playlist_id):
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    session['selected_playlist_id'] = playlist_id
    try: fetch_and_cache_playlist()
    except: pass
    return redirect(url_for('index'))

@app.route('/api/sync', methods=['POST'])
def api_sync():
    try:
        fetch_and_cache_playlist()
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    return '', 204

@app.route('/vote', methods=['POST'])
def vote():
    global DERNIER_RESULTAT, LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    
    id_a, id_b, outcome = request.form.get('id_a'), request.form.get('id_b'), float(request.form.get('outcome'))
    tracks = get_updated_tracks()
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
    tracks = get_updated_tracks()
    sorted_tracks = sorted(tracks.values(), key=lambda x: x['elo'], reverse=True)
    return render_template('classement.html', tracks=sorted_tracks)

@app.route('/api/top5')
def api_top5():
    tracks = get_updated_tracks()
    return jsonify(sorted(tracks.values(), key=lambda x: x['elo'], reverse=True)[:5])

@app.route('/listen/<uri>', methods=['GET', 'POST'])
def listen(uri):
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    try: sp.start_playback(uris=[uri])
    except: pass
    return '', 204

@app.route('/toggle-pause', methods=['POST'])
def toggle_pause():
    global LAST_ACTIVITY_TIME
    LAST_ACTIVITY_TIME = time.time()
    try:
        playback = sp.current_playback()
        if playback and playback.get('is_playing'):
            sp.pause_playback()
            return jsonify({"status": "paused"})
        else:
            sp.start_playback()
            return jsonify({"status": "playing"})
    except: return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    app.run(debug=False, port=5000)