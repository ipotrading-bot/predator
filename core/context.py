import os
import requests

def get_market_news(match_name, sport_title):
    """
    Récupère les actualités récentes pour un match spécifique via NewsAPI.
    """
    api_key = os.environ.get("NEWS_API_KEY")
    if not api_key:
        return "NewsAPI Key manquante."
        
    # On construit une requête précise basée sur les noms d'équipes et le sport
    query = f"{match_name} {sport_title}"
    url = f"https://newsapi.org/v2/everything?q={query}&sortBy=publishedAt&apiKey={api_key}"
    
    try:
        response = requests.get(url)
        data = response.json()
        
        if data['status'] == 'ok' and data['articles']:
            # Retourne les titres des 3 articles les plus récents
            headlines = [article['title'] for article in data['articles'][:3]]
            return ". ".join(headlines)
        else:
            return "Aucune actualité récente trouvée."
    except Exception as e:
        return f"Erreur de récupération des news : {str(e)}"
