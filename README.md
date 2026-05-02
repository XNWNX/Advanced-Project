ID 230103171 230103167 230103330

Backend
The server side is built with Python and Flask — it handles all the routing, user sessions, and page rendering. For the database we went with SQLite, simple and gets the job done for storing reports, users, and history. Passwords are hashed using Werkzeug, and python-dotenv keeps sensitive keys out of the codebase.

AI
We integrated Google Gemini to analyze complaints — it looks at both the text and uploaded photos to help process reports intelligently.

File Storage
Photos uploaded by citizens are stored either locally in static/uploads/ or in an S3-compatible cloud bucket via boto3, depending on how the environment is configured. Pillow handles image processing before saving.

Frontend
Pages are rendered server-side with Jinja2 templates. The UI is built on Bootstrap 5.3 with a fully custom dark theme — deep navy blues, clean cards, and a consistent "Smart City" look throughout. Small bits of vanilla JS handle things like toggling between login and register forms.

Auth
Login is session-based with three roles: citizen, moderator, and akim — each gets their own dashboard and can only see/do what they're supposed to.

Deployment
The app is set up to run on Railway — configured to read the port from environment variables and bind to 0.0.0.0 so it's reachable from the outside.
