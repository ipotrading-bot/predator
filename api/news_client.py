"""
api/news_client.py — News API & RSS Feed Integration
Ingest des news sportives en temps réel pour le contexte des signaux.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional

from config import settings


class NewsClient:
    """Client pour l'ingestion de news sportives."""
    
    def __init__(self):
        self.news_api_key = settings.news_api_key
        self.news_sources = settings.news_sources
        self.rss_feeds = settings.rss_feeds
        self._cache = {}
        self._cache_ttl = 300  # 5 minutes cache
        
    async def get_relevant_news(self, sport: str = None, team: str = None, hours: int = 24) -> list[dict]:
        """
        Récupère les news pertinentes pour un sport/équipe donnée.
        
        Args:
            sport: Sport filter (e.g., "basketball", "soccer")
            team: Team/player filter
            hours: Time window in hours
        
        Returns:
            list[dict]: List of relevant news items
        """
        cache_key = f"{sport}_{team}_{hours}"
        
        # Check cache
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                return cached_data
        
        news_items = []
        
        # Fetch from NewsAPI
        if self.news_api_key:
            api_news = await self._fetch_newsapi(sport, team, hours)
            news_items.extend(api_news)
        
        # Fetch from RSS feeds
        rss_news = await self._fetch_rss_feeds(sport, team)
        news_items.extend(rss_news)
        
        # Sort by recency
        news_items.sort(key=lambda x: x.get("published", ""), reverse=True)
        
        # Deduplicate
        seen_titles = set()
        unique_news = []
        for item in news_items:
            title_key = item["title"].lower()
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                unique_news.append(item)
        
        # Cache results
        self._cache[cache_key] = (time.time(), unique_news[:20])
        
        return unique_news[:20]
    
    async def analyze_news_context(self, signal_data: dict) -> dict:
        """
        Analyse le contexte news pour un signal donné.
        
        Args:
            signal_data: Dict avec event_name, sport, selection
        
        Returns:
            dict: {"has_relevant_news": bool, "sentiment": str, "news_count": int, "key_news": list}
        """
        event_name = signal_data.get("event_name", "")
        sport = signal_data.get("sport", "")
        selection = signal_data.get("selection", "")
        
        # Extract teams/players from event name
        teams = self._extract_teams(event_name)
        
        # Fetch relevant news
        news = await self.get_relevant_news(sport, None, hours=6)
        
        # Filter for relevant news
        relevant_news = []
        for item in news:
            title_lower = item["title"].lower()
            description_lower = item.get("description", "").lower()
            
            # Check if news mentions our teams/players
            is_relevant = False
            for team in teams:
                if team.lower() in title_lower or team.lower() in description_lower:
                    is_relevant = True
                    break
            
            if selection and selection.lower() in title_lower:
                is_relevant = True
            
            if is_relevant:
                relevant_news.append(item)
        
        # Analyze sentiment (simple keyword-based)
        sentiment = self._analyze_sentiment(relevant_news)
        
        return {
            "has_relevant_news": len(relevant_news) > 0,
            "sentiment": sentiment,
            "news_count": len(relevant_news),
            "key_news": relevant_news[:5],
            "analyzed_at": datetime.now().isoformat()
        }
    
    async def get_market_moving_news(self, hours: int = 6) -> list[dict]:
        """
        Récupère les news qui peuvent impacter les cotes.
        """
        # Keywords that typically move markets
        market_moving_keywords = [
            "injury", "injured", "out", "questionable", "doubtful",
            "suspended", "ban", "red card",
            "lineup", "starting xi", "confirmed",
            "weather", "rain", "snow", "postponed",
            "coach", "manager", "fired", "resigned",
            "strike", "protest", "controversy"
        ]
        
        news = await self.get_relevant_news(None, None, hours)
        
        moving_news = []
        for item in news:
            title_lower = item["title"].lower()
            description_lower = item.get("description", "").lower()
            text = title_lower + " " + description_lower
            
            for keyword in market_moving_keywords:
                if keyword in text:
                    moving_news.append(item)
                    break
        
        return moving_news[:10]
    
    async def _fetch_newsapi(self, sport: str, team: str, hours: int) -> list[dict]:
        """Fetch news from NewsAPI.org."""
        if not self.news_api_key:
            return []

        import httpx
        
        url = "https://newsapi.org/v2/everything"
        
        # Build query
        q_parts = []
        if sport:
            q_parts.append(sport)
        if team:
            q_parts.append(team)
        
        # Add sports sources
        q_parts.extend(self.news_sources)
        
        query = " OR ".join(q_parts) if q_parts else "sports"
        
        from_date = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        
        params = {
            "q": query,
            "from": from_date,
            "sortBy": "relevancy",
            "language": "en",
            "apiKey": self.news_api_key,
            "pageSize": 20
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params, timeout=10)
                data = response.json()
                
                if data.get("status") == "ok":
                    return [
                        {
                            "title": article["title"],
                            "description": article.get("description", ""),
                            "source": article["source"]["name"],
                            "url": article["url"],
                            "published": article["publishedAt"],
                            "image": article.get("urlToImage", "")
                        }
                        for article in data.get("articles", [])
                        if article["title"] != "[Removed]"
                    ]
        except Exception as e:
            pass
        
        return []
    
    async def _fetch_rss_feeds(self, sport: str, team: str) -> list[dict]:
        """Fetch news from RSS feeds."""
        import feedparser

        news_items = []
        
        for feed_url in self.rss_feeds:
            try:
                feed = feedparser.parse(feed_url)
                
                for entry in feed.entries[:10]:
                    title = entry.get("title", "")
                    description = entry.get("description", entry.get("summary", ""))
                    published = entry.get("published", "")
                    
                    # Parse published date
                    parsed_date = None
                    if published:
                        try:
                            parsed_date = datetime(*entry.published_parsed[:6]).isoformat()
                        except:
                            parsed_date = published
                    
                    news_items.append({
                        "title": title,
                        "description": description[:200] if description else "",
                        "source": feed.feed.get("title", "RSS"),
                        "url": entry.get("link", ""),
                        "published": parsed_date,
                        "image": ""
                    })
                    
            except Exception as e:
                continue
        
        return news_items
    
    @staticmethod
    def _extract_teams(event_name: str) -> list[str]:
        """Extract team names from event string like 'Lakers vs Nuggets'."""
        if "vs" in event_name:
            parts = event_name.split("vs")
            return [p.strip() for p in parts]
        elif " - " in event_name:
            parts = event_name.split(" - ")
            return [p.strip() for p in parts]
        return [event_name]
    
    @staticmethod
    def _analyze_sentiment(news_items: list[dict]) -> str:
        """
        Simple sentiment analysis based on keywords.
        Returns: "positive", "negative", "neutral", "mixed"
        """
        if not news_items:
            return "neutral"
        
        positive_keywords = [
            "confirmed", "ready", "fit", "return", "win", "victory",
            "strong", "dominant", "excellent", "great", "success"
        ]
        negative_keywords = [
            "injury", "injured", "out", "doubtful", "questionable",
            "loss", "defeat", "poor", "weak", "fail", "suspended",
            "controversy", "scandal"
        ]
        
        positive_count = 0
        negative_count = 0
        
        for item in news_items:
            text = (item["title"] + " " + item.get("description", "")).lower()
            
            for kw in positive_keywords:
                if kw in text:
                    positive_count += 1
            
            for kw in negative_keywords:
                if kw in text:
                    negative_count += 1
        
        if positive_count > negative_count * 1.5:
            return "positive"
        elif negative_count > positive_count * 1.5:
            return "negative"
        elif positive_count > 0 and negative_count > 0:
            return "mixed"
        
        return "neutral"
    
    def get_ticker_items(self) -> list[dict]:
        """
        Format news for the market ticker display.
        """
        # Return cached or sample items for immediate display
        sample_items = [
            {"time": "10:35:01", "message": "🚨 NBA: LeBron James questionable - ankle injury"},
            {"time": "10:34:45", "message": "⚽ Premier League: Haaland confirmed starter vs Arsenal"},
            {"time": "10:33:22", "message": "🏀 NBA: Lakers vs Nuggets - Over 225.5 points trending"},
            {"time": "10:32:10", "message": "🎾 ATP: Djokovic withdraws from Rome Masters"},
            {"time": "10:31:05", "message": "⚽ La Liga: Rain expected - El Clasico under 2.5 goals EV+"},
            {"time": "10:30:00", "message": "🏒 NHL: Bruins goalie injured - backup confirmed"},
        ]
        return sample_items


# Singleton
news_client = NewsClient()