const availableAccentColors = ['green', 'cyan', 'pink', 'orange', 'purple', 'yellow'];

// Appliqué immédiatement dès que le DOM est prêt
document.addEventListener('DOMContentLoaded', () => {
    initThemeAndMode();
});

function initThemeAndMode() {
    // 1. Mode de fond (Sera lu depuis localStorage pour une réactivité instantanée)
    let savedBgMode = localStorage.getItem('elotify_bg_mode') || 'dark';
    document.documentElement.setAttribute('data-bg-mode', savedBgMode);

    // 2. Couleur d'accent
    let savedAccent = localStorage.getItem('elotify_theme') || 'green';
    
    if (savedAccent === 'random') {
        let randomColor = availableAccentColors[Math.floor(Math.random() * availableAccentColors.length)];
        document.documentElement.setAttribute('data-theme', randomColor);
    } else {
        document.documentElement.setAttribute('data-theme', savedAccent);
    }
}

function setBgMode(mode) {
    document.documentElement.setAttribute('data-bg-mode', mode);
    localStorage.setItem('elotify_bg_mode', mode);
    fetch('/set_bg_mode/' + mode, { method: 'POST' }).catch(err => {});
}

function setAccentColor(color) {
    localStorage.setItem('elotify_theme', color);
    
    if (color === 'random') {
        let randomColor = availableAccentColors[Math.floor(Math.random() * availableAccentColors.length)];
        document.documentElement.setAttribute('data-theme', randomColor);
    } else {
        document.documentElement.setAttribute('data-theme', color);
    }

    fetch('/set_theme/' + color, { method: 'POST' }).catch(err => console.error('Erreur sauvegarde thème BDD :', err));
}

function toggleProfileDropdown() {
    const dropdown = document.getElementById('profileDropdown');
    if (dropdown) dropdown.classList.toggle('show');
}

window.onclick = function(event) {
    if (!event.target.closest('.user-profile-container')) {
        const dropdown = document.getElementById('profileDropdown');
        if (dropdown && dropdown.classList.contains('show')) {
            dropdown.classList.remove('show');
        }
    }
}