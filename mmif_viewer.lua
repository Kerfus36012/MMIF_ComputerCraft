-- mmif_player.lua (исправленный - всегда 48kHz)
local colors = {
    colors.white, colors.orange, colors.magenta, colors.lightBlue,
    colors.yellow, colors.lime, colors.pink, colors.gray,
    colors.lightGray, colors.cyan, colors.purple, colors.blue,
    colors.brown, colors.green, colors.red, colors.black
}

-- Функция для преобразования 4 байт в число (big-endian)
local function bytesToNumber(b1, b2, b3, b4)
    return (b1 * 256^3) + (b2 * 256^2) + (b3 * 256) + b4
end

local function bytesToNumber2(b1, b2)
    return (b1 * 256) + b2
end

local function playMMIF(filename)
    local file = fs.open(filename, "rb")
    if not file then error("Cannot open file") end
    
    -- Чтение заголовка
    local sig = file.read(4)
    if sig ~= "MMIF" then file.close(); error("Invalid MMIF") end
    
    local width = bytesToNumber2(string.byte(file.read(1)), string.byte(file.read(1)))
    local height = bytesToNumber2(string.byte(file.read(1)), string.byte(file.read(1)))
    local flags = string.byte(file.read(1))
    local fps = string.byte(file.read(1))
    
    local isVideo = bit32.band(flags, 0x01) ~= 0
    local isLooped = bit32.band(flags, 0x02) ~= 0
    local hasExtended = bit32.band(flags, 0x80) ~= 0
    
    local audioData = nil
    local videoDataStart = 10
    
    if hasExtended then
        -- Читаем расширенный заголовок (32 байта)
        local audio_flags = string.byte(file.read(1))
        local audio_rate_byte = string.byte(file.read(1))
        local audio_channels = string.byte(file.read(1))
        
        file.read(5) -- reserved
        
        -- audio_data_offset
        local a_off = {string.byte(file.read(1)), string.byte(file.read(1)), 
                       string.byte(file.read(1)), string.byte(file.read(1))}
        local audioDataStart = bytesToNumber(a_off[1], a_off[2], a_off[3], a_off[4])
        
        -- audio_data_size
        local a_sz = {string.byte(file.read(1)), string.byte(file.read(1)),
                      string.byte(file.read(1)), string.byte(file.read(1))}
        local audioDataSize = bytesToNumber(a_sz[1], a_sz[2], a_sz[3], a_sz[4])
        
        -- video_data_offset
        local v_off = {string.byte(file.read(1)), string.byte(file.read(1)),
                       string.byte(file.read(1)), string.byte(file.read(1))}
        videoDataStart = bytesToNumber(v_off[1], v_off[2], v_off[3], v_off[4])
        
        -- video_data_size
        local v_sz = {string.byte(file.read(1)), string.byte(file.read(1)),
                      string.byte(file.read(1)), string.byte(file.read(1))}
        
        -- total_duration_ms
        local dur = {string.byte(file.read(1)), string.byte(file.read(1)),
                     string.byte(file.read(1)), string.byte(file.read(1))}
        
        file.read(8) -- reserved2
        
        if bit32.band(audio_flags, 0x01) ~= 0 and audioDataSize > 0 then
            -- ВНИМАНИЕ: speaker всегда работает на 48000 Гц!
            -- Игнорируем audio_rate_byte и используем 48000
            audioData = { 
                start = audioDataStart, 
                size = audioDataSize, 
                rate = 48000  -- Всегда 48kHz для CC:Tweaked
            }
            print("Audio found: " .. audioDataSize .. " bytes, 48000 Hz (fixed)")
        end
    end
    
    -- Сканируем позиции кадров
    local framePos = {}
    local frameCount = 0
    file.seek("set", videoDataStart)
    
    while true do
        local marker = file.read(1)
        if not marker then break end
        if string.byte(marker) == 0xAD then
            frameCount = frameCount + 1
            framePos[frameCount] = file.seek("cur") - 1
            file.seek("cur", width * height)
        elseif string.byte(marker) == 0xFF then
            break
        end
    end
    
    file.close()
    
    if frameCount == 0 then error("No video frames found") end
    
    print(string.format("Loaded: %d frames, %d FPS, %dx%d", frameCount, fps, width, height))
    
    -- === Воспроизведение ===
    term.setGraphicsMode(1)
    term.clear()
    
    local frameDelay = 1 / fps
    local currentFrame = 1
    local speaker = peripheral.find("speaker")
    
    if not speaker then
        print("Warning: No speaker found, playing without audio")
    end
    
    -- Функция воспроизведения DFPWM (правильная для 48kHz)
    local function playAudioStream()
        if not speaker or not audioData then return end
        
        local dfpwm = nil
        local decoder = nil
        
        -- Загружаем DFPWM модуль
        local success, dfpwm_module = pcall(require, "cc.audio.dfpwm")
        if success then
            dfpwm = dfpwm_module
            decoder = dfpwm.make_decoder()
            print("DFPWM decoder loaded")
        else
            print("Error: DFPWM module not available")
            return
        end
        
        local audioFile = fs.open(filename, "rb")
        if not audioFile then
            print("Error: Cannot open audio data")
            return
        end
        
        audioFile.seek("set", audioData.start)
        
        -- Читаем DFPWM чанками по 16KB (как рекомендовано в документации)
        local chunkSize = 16 * 1024
        local remaining = audioData.size
        
        print("Starting audio streaming...")
        
        while remaining > 0 do
            local readSize = math.min(chunkSize, remaining)
            local audioChunk = audioFile.read(readSize)
            if not audioChunk then break end
            
            -- Декодируем DFPWM в PCM буфер (8-bit, -128..127)
            local pcmBuffer = decoder(audioChunk)
            
            -- Отправляем в динамик, ожидая освобождения буфера
            while not speaker.playAudio(pcmBuffer) do
                os.pullEvent("speaker_audio_empty")
            end
            
            remaining = remaining - readSize
        end
        
        audioFile.close()
        print("Audio streaming finished")
    end
    
    -- Функция отображения кадра
    local function displayFrame(frameNum)
        local f = fs.open(filename, "rb")
        if not f then return false end
        
        f.seek("set", framePos[frameNum])
        f.read(1) -- пропускаем 0xAD
        
        for y = 1, height do
            for x = 1, width do
                local pixel = f.read(1)
                if pixel then
                    local colorIndex = string.byte(pixel)
                    if colorIndex and colors[colorIndex + 1] then
                        term.setPixel(x, y, colors[colorIndex + 1])
                    end
                end
            end
        end
        
        f.close()
        return true
    end
    
    -- Запускаем аудио и видео параллельно
    local audioThread = nil
    if audioData and speaker then
        audioThread = function()
            playAudioStream()
        end
    end
    
    print("Playing video... Press Ctrl+T to stop")
    
    if audioThread and parallel then
        -- Параллельное воспроизведение
        parallel.waitForAny(audioThread, function()
            while true do
                local frameStart = os.clock()
                
                if not displayFrame(currentFrame) then
                    break
                end
                
                currentFrame = currentFrame + 1
                if currentFrame > frameCount then
                    if isLooped then
                        currentFrame = 1
                    else
                        break
                    end
                end
                
                local elapsed = os.clock() - frameStart
                local waitTime = frameDelay - elapsed
                if waitTime > 0.001 then
                    sleep(waitTime)
                end
            end
        end)
    else
        -- Только видео
        while true do
            local frameStart = os.clock()
            
            if not displayFrame(currentFrame) then
                break
            end
            
            currentFrame = currentFrame + 1
            if currentFrame > frameCount then
                if isLooped then
                    currentFrame = 1
                else
                    break
                end
            end
            
            local elapsed = os.clock() - frameStart
            local waitTime = frameDelay - elapsed
            if waitTime > 0.001 then
                sleep(waitTime)
            end
        end
    end
    
    if speaker then
        speaker.stopAll()
    end
    
    term.setGraphicsMode(0)
    print("Playback finished")
end

-- Точка входа
local args = {...}
if #args == 0 then
    print("=== MMIF Player (CC:Tweaked optimized) ===")
    print("Usage: mmif_player <file.mmif>")
    print()
    print("Audio is always played at 48kHz (CC:Tweaked requirement)")
    print("DFPWM decoding uses built-in cc.audio.dfpwm")
    print()
    return
end

local filename = args[1]
if not fs.exists(filename) then
    print("File not found: " .. filename)
    return
end

local ok, err = pcall(playMMIF, filename)
if not ok then
    print("Error: " .. err)
end

term.setGraphicsMode(0)
