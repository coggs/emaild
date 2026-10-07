-- "Seen it": clear an email from Needs attention without changing emAIl's verdict (so it isn't a training signal).
ALTER TABLE decisions ADD (dismissed_at TIMESTAMP WITH TIME ZONE)
/
