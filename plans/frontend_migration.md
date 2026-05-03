# Frontend Architecture Plan: Predator UI

## Goal
Replace the deprecated Streamlit interface with a modern, robust frontend using Next.js (React) to align with the existing `ui/` directory structure.

## Architecture
- **Framework**: Next.js (App Router)
- **Styling**: Tailwind CSS
- **Components**: React Functional Components (reusing existing `ui/components/`)
- **API Integration**: Client-side fetching from the `api/` (Python serverless functions).
- **State Management**: React Context or SWR for data fetching.

## Roadmap
1. [ ] Configure `package.json` with necessary Next.js dependencies.
2. [ ] Refine `ui/app/layout.tsx` to include global navigation (`ui/components/Sidebar.tsx`).
3. [ ] Implement data fetching logic in `ui/app/` pages using `fetch` to interact with backend API.
4. [ ] Integrate existing UI components (`ui/components/*`) into the new page structure.
5. [ ] Perform thorough testing to ensure functional parity with the former Streamlit dashboard.
