--[[
MMIF v2
signature   = 4 bytes : "MMIF"
width       = 2 bytes : big-endian
height      = 2 bytes : big-endian
fps         = 1 byte

flags       = 1 byte
    xxxxxxx0 : isLooping
    xxxxxx0x : isVideo
    xxxxx0xx : isCompressed
    xxxx0xxx : hasAudio

если hasAudio:
    audioLength = 4 bytes : big-endian
    audioRate   = 2 bytes : big-endian (обычно 48000)

audio data   = audioLength bytes (DFPWM)

frames:
    AD           = frame start
    ...          = 2 pixels per byte (или RLE, если isCompressed)
FF           = End Of File
]]--

local dfpwm = require("cc.audio.dfpwm")

local color_lib = {
    colors.white, colors.orange, colors.magenta, colors.lightBlue,
    colors.yellow, colors.lime, colors.pink, colors.gray,
    colors.lightGray, colors.cyan, colors.purple, colors.blue,
    colors.brown, colors.green, colors.red, colors.black
}

local function bytesToNumber4(b1, b2, b3, b4)
    return (b1 * 256^3) + (b2 * 256^2) + (b3 * 256) + b4
end

local function bytesToNumber2(b1, b2)
    return (b1 * 256) + b2
end

-- ─────────────────────────────────────────────────────────────────────
-- Заголовок
-- ─────────────────────────────────────────────────────────────────────
local function file_head_reader(filename)
    local file = fs.open(filename, "rb")
    if not file then
        return {OK=false, errmsg="ERR: file not exists: "..filename}
    end

    if file.read(4) ~= "MMIF" then
        file.close()
        return {OK=false, errmsg="ERR: Not a valid MMIF file"}
    end

    local w1 = string.byte(file.read(1))
    local w2 = string.byte(file.read(1))
    local width = bytesToNumber2(w1, w2)

    local h1 = string.byte(file.read(1))
    local h2 = string.byte(file.read(1))
    local height = bytesToNumber2(h1, h2)

    local fps = string.byte(file.read(1))
    local flagByte = string.byte(file.read(1))

    local isLooped     = bit.band(flagByte, 0x01) ~= 0
    local isVideo      = bit.band(flagByte, 0x02) ~= 0
    local isCompressed = bit.band(flagByte, 0x04) ~= 0
    local hasAudio     = bit.band(flagByte, 0x08) ~= 0

    local audioLength, audioRate = 0, 0
    local audioOffset = 0
    local headerSize = 10

    if hasAudio then
        local a1 = string.byte(file.read(1))
        local a2 = string.byte(file.read(1))
        local a3 = string.byte(file.read(1))
        local a4 = string.byte(file.read(1))
        audioLength = bytesToNumber4(a1, a2, a3, a4)

        local r1 = string.byte(file.read(1))
        local r2 = string.byte(file.read(1))
        audioRate = bytesToNumber2(r1, r2)

        audioOffset = 16            -- 4+2+2+1+1 + 4+2
        headerSize  = audioOffset + audioLength
    end

    file.close()
    return {
        OK=true,
        width=width, height=height, fps=fps,
        isVideo=isVideo, isLooped=isLooped, isCompressed=isCompressed,
        hasAudio=hasAudio, audioLength=audioLength, audioRate=audioRate,
        audioOffset=audioOffset, headerSize=headerSize,
    }
end

-- ─────────────────────────────────────────────────────────────────────
-- Отрисовка одного кадра из буфера байтов
-- ─────────────────────────────────────────────────────────────────────
local function blit_frame(buf, width, height, odd_width)
    local bytes_per_row = math.floor(width / 2)
    local idx = 0
    for H = 1, height do
        for B = 1, bytes_per_row do
            idx = idx + 1
            local fb = buf[idx] or 0
            local hi = bit.brshift(bit.band(0xF0, fb), 4) + 1
            local lo = bit.band(0x0F, fb) + 1
            local W = (B - 1) * 2 + 1
            term.setPixel(W,     H, color_lib[hi])
            term.setPixel(W + 1, H, color_lib[lo])
        end
        if odd_width then
            idx = idx + 1
            local fb = buf[idx] or 0
            local hi = bit.brshift(bit.band(0xF0, fb), 4) + 1
            term.setPixel(width, H, color_lib[hi])
        end
    end
end

-- ─────────────────────────────────────────────────────────────────────
-- Чтение одного кадра из video-дескриптора
--   возвращает буфер байтов (длина = bytes_per_row*height)
-- ─────────────────────────────────────────────────────────────────────
local function read_frame(file, width, height, compressed)
    local bytes_per_row = math.floor(width / 2)
    local total_bytes = bytes_per_row * height

    if not compressed then
        local buf = {}
        for i = 1, total_bytes do
            local ch = file.read(1)
            if not ch then return nil end
            buf[i] = string.byte(ch)
        end
        return buf
    end

    -- RLE
    local buf = {}
    local produced = 0
    while produced < total_bytes do
        local vch = file.read(1); if not vch then return nil end
        local cch = file.read(1); if not cch then return nil end
        local val = string.byte(vch)
        local cnt = string.byte(cch)
        for i = 1, cnt do
            produced = produced + 1
            buf[produced] = val
        end
    end
    return buf
end

-- ─────────────────────────────────────────────────────────────────────
-- Главный цикл: видео + аудио синхронно
-- ─────────────────────────────────────────────────────────────────────
local function play(filename, header)
    local video = fs.open(filename, "rb")
    if not video then print("ERR: cannot open "..filename); return end

    local width, height = header.width, header.height
    local fps, looped   = header.fps, header.isLooped
    local compressed    = header.isCompressed
    local odd_width     = (width % 2) == 1
    local bytes_per_row = math.floor(width / 2)

    -- Спикер и аудио-дескриптор
    local speaker = nil
    local audio_file = nil
    local decoder = nil
    local audio_bytes_per_frame = 0
    local audio_remaining = 0

    if header.hasAudio and header.audioLength > 0 then
        speaker = peripheral.find("speaker")
        if speaker then
            audio_file = fs.open(filename, "rb")
            if audio_file then
                audio_file.seek("set", header.audioOffset)
                decoder = dfpwm.make_decoder()
                audio_bytes_per_frame = math.max(1,
                    math.floor(header.audioRate / fps / 8))
                audio_remaining = header.audioLength
            end
        end
    end

    -- Тайминг для видео (если аудио нет)
    local frame_time = 1 / fps
    local start_time = os.clock()

    while true do
        -- Перемотка в начало кадров
        video.seek("set", header.headerSize)
        if audio_file then
            audio_file.seek("set", header.audioOffset)
            audio_remaining = header.audioLength
        end
        start_time = os.clock()

        local saw_frame = false

        while true do
            local marker = video.read(1)
            if not marker then break end
            local mb = string.byte(marker)

            if mb == 0xFF then
                break
            elseif mb == 0xAD then
                saw_frame = true

                -- ── 1. Аудио-чанк для этого кадра ───────────────
                if speaker and decoder and audio_remaining > 0 then
                    local toRead = math.min(audio_bytes_per_frame, audio_remaining)
                    local chunk = audio_file.read(toRead)
                    if chunk and #chunk > 0 then
                        audio_remaining = audio_remaining - #chunk
                        local buffer = decoder(chunk)
                        while not speaker.playAudio(buffer, 1.0) do
                            os.pullEvent("speaker_audio_empty")
                        end
                    end
                end

                -- ── 2. Рисуем кадр ──────────────────────────────
                local buf = read_frame(video, width, height, compressed)
                if not buf then break end

                blit_frame(buf, width, height, odd_width)

                -- ── 3. Синхронизация ────────────────────────────
                if speaker then
                    -- Ждём, пока спикер освободит буфер.
                    -- Это и yield, и тик кадра.
                    os.pullEvent("speaker_audio_empty")
                else
                    -- Без аудио — таймер по os.clock()
                    local target = os.clock() - start_time
                    local wait = frame_time - target
                    if wait > 0 then
                        sleep(wait)
                    else
                        sleep(0)
                    end
                    start_time = os.clock()
                end
            end
        end

        if not looped or not saw_frame then break end
    end

    video.close()
    if audio_file then audio_file.close() end
end

-- ─────────────────────────────────────────────────────────────────────
-- Точка входа
-- ─────────────────────────────────────────────────────────────────────
local function draw_init(filename)
    local h = file_head_reader(filename)
    if not h.OK then print(h.errmsg); return end

    print("Audio rate:", h.audioRate)
    print("Audio length:", h.audioLength)
    print("")
    print("W/H", h.width..'/'..h.height)
    print("flags:", h.isLooped, "|", h.isVideo, "|", h.isCompressed, "|", h.hasAudio)
    print("      looped | video | comp... | audio")
    print("fps:", h.fps)
    sleep(1)

    term.setGraphicsMode(1)
    term.clear()

    play(filename, h)
end

-- ─────────────────────────────────────────────────────────────────────
-- CLI
-- ─────────────────────────────────────────────────────────────────────
local args = {...}
if #args == 0 then
    print("=== MMIF Player (CC:Tweaked) ===")
    print("Usage: mmif_player <file.mmif>")
    print()
    return
end

local filename = args[1]
if not fs.exists(filename) then
    print("File not found: " .. filename)
    return
end

local ok, err = pcall(draw_init, filename)
if not ok then
    print("Error: " .. err)
end

term.setGraphicsMode(0)