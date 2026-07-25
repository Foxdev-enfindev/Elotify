import random
import os
import json
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# --- CONFIGURATION SPOTIFY ---
CLIENT_ID = "b969cdabdc8443afb3bc0f494f0513bc"
CLIENT_SECRET = "6c3bc4b35b474028b605d9f358c230d4"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
PLAYLIST_ID = "4xutwP8gRPlj1YTvJmysaH"

DATA_FILE = "classement_elo.json"
K_FACTOR = 32
DEFAULT_RATING = 1000

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri=REDIRECT_URI,
    scope="playlist-read-private playlist-read-collaborative user-modify-playback-state user-read-playback-state"
))

def load_local_scores():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_local_scores(tracks_data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(tracks_data, f, ensure_ascii=False, indent=4)
    print(f"💾 Progression sauvegardée ({len(tracks_data)} morceaux).")

def get_updated_tracks(playlist_id):
    local_scores = load_local_scores()
    tracks = {}
    offset = 0
    limit = 100

    while True:
        try:
            results = sp.playlist_items(playlist_id, limit=limit, offset=offset)
        except Exception as e:
            print(f"❌ Erreur lors de la requête Spotify : {e}")
            break
        
        if not results or 'items' not in results:
            break
            
        items = results['items']
        if len(items) == 0:
            break
            
        for item in items:
            if not item or 'item' not in item or not item['item']:
                continue
                
            track = item['item']
            track_id = track.get('id')
            
            if not track_id:
                continue
                
            artists = track.get('artists')
            artist_name = artists[0].get('name', 'Artiste inconnu') if artists else 'Artiste inconnu'
            
            current_elo = local_scores.get(track_id, {}).get("elo", DEFAULT_RATING)
            
            tracks[track_id] = {
                "name": track.get('name', 'Titre inconnu'),
                "artist": artist_name,
                "uri": track.get('uri'),
                "elo": current_elo
            }
                
        if not results.get('next'):
            break
        offset += limit

    return tracks

def play_track(track_uri):
    try:
        sp.start_playback(uris=[track_uri])
    except:
        pass

def calculate_expected_score(rating_a, rating_b):
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))

def update_elo(rating_a, rating_b, outcome_a):
    expected_a = calculate_expected_score(rating_a, rating_b)
    expected_b = calculate_expected_score(rating_b, rating_a)
    outcome_b = 1 - outcome_a
    new_rating_a = rating_a + K_FACTOR * (outcome_a - expected_a)
    new_rating_b = rating_b + K_FACTOR * (outcome_b - expected_b)
    return round(new_rating_a), round(new_rating_b)

def print_leaderboard(tracks):
    """Affiche le Top 20 actuel des morceaux."""
    print("\n" + "="*20 + " TOP 20 EN DIRECT " + "="*20)
    sorted_tracks = sorted(tracks.values(), key=lambda x: x['elo'], reverse=True)
    for i, track in enumerate(sorted_tracks[:20], 1):
        print(f"{i:2d}. [{track['elo']}] {track['name']} — {track['artist']}")
    print("="*58)

def ask_to_quit():
    confirm = input("⚠️ Êtes-vous sûr de vouloir quitter ? (o/n) : ").strip().lower()
    if confirm == 'o':
        try: sp.pause_playback()
        except: pass
        return True
    return False

def main():
    tracks = get_updated_tracks(PLAYLIST_ID)
    track_ids = list(tracks.keys())

    if len(track_ids) < 2:
        print("❌ La playlist ne contient pas assez de morceaux valides.")
        return

    print(f"✅ {len(track_ids)} morceaux synchronisés. Prêt pour les duels !")
    print("Commandes : '1' ou '2' pour voter | '3' pour match nul | [ENTRÉE] pour alterner | '9' pour voir le Top | '0' pour quitter")
    print("-" * 110)

    try:
        while True:
            id_a, id_b = random.sample(track_ids, 2)
            track_a = tracks[id_a]
            track_b = tracks[id_b]
            
            print(f"\n--- DUEL ---")
            print(f"🔊 En cours : [1] : {track_a['name']} — {track_a['artist']} (Elo: {track_a['elo']})")
            play_track(track_a['uri'])
            
            current_playing = 1
            
            # Phase unique de comparaison et de vote
            while True:
                choice = input("Votre choix ? (1 / 2 / 3 / [ENTRÉE]: Alterner / 9: Classement / 0: Quitter) : ").strip()
                
                if choice == "":
                    if current_playing == 1:
                        print(f"🔄 Bascule sur le Titre [2] : {track_b['name']} — {track_b['artist']}")
                        play_track(track_b['uri'])
                        current_playing = 2
                    else:
                        print(f"🔄 Retour sur le Titre [1] : {track_a['name']} — {track_a['artist']}")
                        play_track(track_a['uri'])
                        current_playing = 1
                    continue
                    
                elif choice == '9':
                    try: sp.pause_playback()
                    except: pass
                    print_leaderboard(tracks)
                    input("\nPressez [ENTRÉE] pour reprendre le duel et relancer la musique...")
                    # Relance la musique là où le joueur en était
                    if current_playing == 1:
                        play_track(track_a['uri'])
                    else:
                        play_track(track_b['uri'])
                    continue

                elif choice == '0':
                    if ask_to_quit():
                        return
                    else:
                        print("▶️ Reprise du duel en cours...")
                        if current_playing == 1: play_track(track_a['uri'])
                        else: play_track(track_b['uri'])
                        continue

                elif choice in ['1', '2', '3']:
                    break
                else:
                    print("Choix invalide. (Options : 1, 2, 3, 9, 0 ou [ENTRÉE])")
            
            # Calcul des variations de score ELO
            old_elo_a = track_a['elo']
            old_elo_b = track_b['elo']

            if choice == '1':
                new_elo_a, new_elo_b = update_elo(old_elo_a, old_elo_b, 1)
                tracks[id_a]['elo'], tracks[id_b]['elo'] = new_elo_a, new_elo_b
                print(f"-> Gagnant : {track_a['name']}.")
                
            elif choice == '2':
                new_elo_a, new_elo_b = update_elo(old_elo_a, old_elo_b, 0)
                tracks[id_a]['elo'], tracks[id_b]['elo'] = new_elo_a, new_elo_b
                print(f"-> Gagnant : {track_b['name']}.")
                
            elif choice == '3':
                new_elo_a, new_elo_b = update_elo(old_elo_a, old_elo_b, 0.5)
                tracks[id_a]['elo'], tracks[id_b]['elo'] = new_elo_a, new_elo_b
                print(f"-> Match nul.")

            # Affichage de la variation ELO après le duel
            diff_a = new_elo_a - old_elo_a
            diff_b = new_elo_b - old_elo_b
            sign_a = "+" if diff_a >= 0 else ""
            sign_b = "+" if diff_b >= 0 else ""
            
            print(f"📊 Évolution ELO :")
            print(f"   [1] {track_a['name']} : {old_elo_a} -> {new_elo_a} ({sign_a}{diff_a})")
            print(f"   [2] {track_b['name']} : {old_elo_b} -> {new_elo_b} ({sign_b}{diff_b})")
                
    finally:
        if len(tracks) >= 2:
            save_local_scores(tracks)

        # Affichage complet et définitif du classement général en quittant
        print("\n" + "="*23 + " CLASSEMENT GÉNÉRAL COMPLET " + "="*23)
        sorted_tracks = sorted(tracks.values(), key=lambda x: x['elo'], reverse=True)
        for i, track in enumerate(sorted_tracks, 1):
            print(f"{i:3d}. [{track['elo']}] {track['name']} — {track['artist']}")
        print("="*74)

if __name__ == "__main__":
    main()