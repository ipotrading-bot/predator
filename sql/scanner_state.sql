-- ═══════════════════════════════════════════════════════════════
-- PAIM v3.6 — Protocole de Rotation de Scan (State-Based)
-- PhD MIT Architecture — Rate Limiting Optimization
-- ═══════════════════════════════════════════════════════════════

-- Table de contrôle de rotation circulaire
-- Permet de scanner un sport à la fois toutes les 4-5 minutes
-- Respecte la limite 15 requêtes/minute de The-Odds-API

CREATE TABLE IF NOT EXISTS public.scanner_state (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_index INTEGER DEFAULT 0,
    current_sport TEXT DEFAULT '',
    scan_count INTEGER DEFAULT 0,
    last_scan_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Initialisation (évite les erreurs de duplication)
INSERT INTO public.scanner_state (id, last_index, current_sport, scan_count) 
VALUES (1, 0, '', 0) 
ON CONFLICT (id) DO NOTHING;

-- Index pour performances (même si table petite)
CREATE INDEX IF NOT EXISTS idx_scanner_state_updated 
ON public.scanner_state(updated_at);

-- Vue de monitoring (optionnel)
CREATE OR REPLACE VIEW public.scan_rotation_log AS
SELECT 
    id,
    last_index,
    current_sport,
    scan_count,
    last_scan_at,
    updated_at,
    EXTRACT(EPOCH FROM (now() - last_scan_at))::INTEGER as seconds_since_last_scan
FROM public.scanner_state
WHERE id = 1;

-- Commentaires pour documentation
COMMENT ON TABLE public.scanner_state IS 
'Table de contrôle pour la rotation séquentielle des scans PAIM. 
Gère l''état entre les cycles GitHub Actions (toutes les 5 minutes).
MIT PhD Architecture — Rate Limiting: 15 RPM max.';

COMMENT ON COLUMN public.scanner_state.last_index IS 
'Index dans ALPHA_WATCHLIST du dernier sport scanné (0-indexed)';

COMMENT ON COLUMN public.scanner_state.scan_count IS 
'Nombre total de cycles de scan effectués';

-- Permissions (RLS activé par défaut sur Supabase)
ALTER TABLE public.scanner_state ENABLE ROW LEVEL SECURITY;

-- Policy pour service_role (backend Python)
CREATE POLICY "Service role full access" ON public.scanner_state
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- Policy pour anon read-only (monitoring)
CREATE POLICY "Anon read-only" ON public.scanner_state
    FOR SELECT USING (true);
