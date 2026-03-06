@echo off
setlocal
echo Starting Project Bazinga Governance Node...
cd /d C:\Users\heher\Amazon_Nova_AI_Hackathon || (
  echo Project folder not found.
  pause
  exit /b 1
)

echo Checking Bedrock Connectivity...
python src\hero_logic.py
if errorlevel 1 (
  echo Bedrock check failed. Review output above.
  pause
  exit /b 1
)

echo Launching API and Dashboard...
uvicorn src.app:app --reload
