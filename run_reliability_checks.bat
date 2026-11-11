@echo off
setlocal

rem Run the backend and Tk UI smoke tests with the project's virtual environment.

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo Python virtual environment not found at "%PYTHON_EXE%".
    exit /b 1
)

echo [1/5] Compiling reliability check scripts...
"%PYTHON_EXE%" -m py_compile "%~dp0reliability_smoke_test.py" "%~dp0ui_reliability_smoke_test.py" "%~dp0spell_check.py" "%~dp0grammar_check.py" "%~dp0windows_update.py" "%~dp0tests\test_spell_check.py" "%~dp0tests\test_windows_update.py"
if errorlevel 1 exit /b 1

echo [2/5] Running spelling and local grammar regression tests...
pushd "%~dp0"
"%PYTHON_EXE%" -m unittest discover -s tests -p test_spell_check.py
set "SPELL_CHECK_RESULT=%ERRORLEVEL%"
popd
if not "%SPELL_CHECK_RESULT%"=="0" exit /b %SPELL_CHECK_RESULT%

echo [3/5] Running protected Windows update tests...
pushd "%~dp0"
"%PYTHON_EXE%" -m unittest discover -s tests -p test_windows_update.py
set "UPDATE_CHECK_RESULT=%ERRORLEVEL%"
popd
if not "%UPDATE_CHECK_RESULT%"=="0" exit /b %UPDATE_CHECK_RESULT%

echo Running preview image, GIF, video, and Bible layout regression tests...
pushd "%~dp0"
"%PYTHON_EXE%" -m unittest discover -s tests -p test_mirror_bible_columns.py
set "PREVIEW_CHECK_RESULT=%ERRORLEVEL%"
popd
if not "%PREVIEW_CHECK_RESULT%"=="0" exit /b %PREVIEW_CHECK_RESULT%

echo Running seamless video loop regression tests...
pushd "%~dp0"
"%PYTHON_EXE%" -m unittest discover -s tests -p test_video_loop.py
set "VIDEO_LOOP_CHECK_RESULT=%ERRORLEVEL%"
popd
if not "%VIDEO_LOOP_CHECK_RESULT%"=="0" exit /b %VIDEO_LOOP_CHECK_RESULT%

echo Running subscription license regression tests...
pushd "%~dp0"
"%PYTHON_EXE%" -m unittest discover -s tests -p test_licensing.py
set "LICENSE_CHECK_RESULT=%ERRORLEVEL%"
popd
if not "%LICENSE_CHECK_RESULT%"=="0" exit /b %LICENSE_CHECK_RESULT%

echo Running automatic update regression tests...
pushd "%~dp0"
"%PYTHON_EXE%" -m unittest discover -s tests -p test_auto_update.py
set "AUTO_UPDATE_CHECK_RESULT=%ERRORLEVEL%"
popd
if not "%AUTO_UPDATE_CHECK_RESULT%"=="0" exit /b %AUTO_UPDATE_CHECK_RESULT%

echo Running song part regression tests...
pushd "%~dp0"
"%PYTHON_EXE%" -m unittest discover -s tests -p test_song_parts.py
set "SONG_PARTS_CHECK_RESULT=%ERRORLEVEL%"
popd
if not "%SONG_PARTS_CHECK_RESULT%"=="0" exit /b %SONG_PARTS_CHECK_RESULT%

echo [4/5] Running backend reliability smoke test...
"%PYTHON_EXE%" "%~dp0reliability_smoke_test.py"
if errorlevel 1 exit /b 1

echo [5/5] Running UI reliability smoke test...
"%PYTHON_EXE%" "%~dp0ui_reliability_smoke_test.py"
if errorlevel 1 exit /b 1

echo Reliability checks passed.
exit /b 0
