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
    xxxx0xxx : hasAudio        (новое)
    xxx0xxxx : audioCompressed (DFPWM)  (новое)

если hasAudio:
    audioLength = 4 bytes : big-endian, длина DFPWM данных в байтах
    audioRate   = 2 bytes : big-endian, частота (обычно 48000)

audio data   = audioLength bytes

frames:
    AD           = frame start
    ...          = 2 pixels per byte
    AD ...
FF           = End Of File
]]--

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
function file_head_reader(filename)
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

        headerSize = 10 + 6 + audioLength
    end

    file.close()
    return {
        OK=true,
        width=width, height=height, fps=fps,
        isVideo=isVideo, isLooped=isLooped, isCompressed=isCompressed,
        hasAudio=hasAudio, audioLength=audioLength, audioRate=audioRate,
        headerSize=headerSize,
    }
end

-- ─────────────────────────────────────────────────────────────────────
-- Аудио
-- ─────────────────────────────────────────────────────────────────────
local dfpwm = require("cc.audio.dfpwm")

local function play_audio(filename, header, volume)
    if not header.hasAudio or header.audioLength == 0 then return end

    local speaker = peripheral.find("speaker")
    if not speaker then return end

    local file = fs.open(filename, "rb")
    if not file then return end

    local AUDIO_OFFSET = 16
    file.seek("set", AUDIO_OFFSET)

    local decoder = dfpwm.make_decoder()
    local CHUNK = 16 * 1024       -- 16 КБ DFPWM
    local remaining = header.audioLength
    local vol = volume or 1.0

    while remaining > 0 do
        local toRead = math.min(CHUNK, remaining)
        local str = file.read(toRead)
        if not str or #str == 0 then break end
        remaining = remaining - #str

        -- DFPWM → таблица амплитуд -128..127
        local buffer = decoder(str)

        while not speaker.playAudio(buffer, vol) do
            os.pullEvent("speaker_audio_empty")
        end
    end

    file.close()
end

-- ─────────────────────────────────────────────────────────────────────
-- Точка входа
-- ─────────────────────────────────────────────────────────────────────
function draw_init(filename)
    local h = file_head_reader(filename)
    if not h.OK then print(h.errmsg); return end
	
	print("Audio rate:", h.audioRate)
    print("Audio length:", h.audioLength)
	print("")
	print("W/H",h.width..'/'..h.height)
	print("flags:",h.isLooped,"|",h.isVideo,"|",h.isCompressed,"|",h.hasAudio)
	print("      looped | video | comp... | audio")
	print("fps:",h.fps)
	
	sleep(1)
	
    term.setGraphicsMode(1)
    term.clear()

    local video_fn = h.isCompressed and draw_decomp or draw_notComp

    if h.hasAudio then
        parallel.waitForAny(
            function() play_audio(filename, h, 1.0) end,
            function() video_fn(filename, h) end
        )
    else
        video_fn(filename, h)
    end
end

-- ─────────────────────────────────────────────────────────────────────
-- Видео: несжатое
-- ─────────────────────────────────────────────────────────────────────
function draw_notComp(filename, header)
    local file = fs.open(filename, "rb")
    if not file then print("ERR: cannot open "..filename); return end

    local width, height = header.width, header.height
    local fps, looped   = header.fps, header.isLooped
    local HEADER        = header.headerSize

    local bytes_per_row = math.floor(width / 2)
    local odd_width = (width % 2) == 1
	
	local frame_time = 1 / fps
	local start = os.clock()
	local frame_idx = 0
	
    while true do
        file.seek("set", HEADER)
        local saw_frame = false

        while true do
            local marker = file.read(1)
            if not marker then break end
            local mb = string.byte(marker)

            if mb == 0xFF then
                break
            elseif mb == 0xAD then
                saw_frame = true
                for H = 1, height do
                    for B = 1, bytes_per_row do
                        local ch = file.read(1)
                        if not ch then break end
                        local fb = string.byte(ch)
                        local hi = bit.brshift(bit.band(0xF0, fb), 4) + 1
                        local lo = bit.band(0x0F, fb) + 1
                        local W = (B - 1) * 2 + 1
                        term.setPixel(W,     H, color_lib[hi])
                        term.setPixel(W + 1, H, color_lib[lo])
                    end
                    if odd_width then
                        local ch = file.read(1)
                        if ch then
                            local fb = string.byte(ch)
                            local hi = bit.brshift(bit.band(0xF0, fb), 4) + 1
                            term.setPixel(width, H, color_lib[hi])
                        end
                    end
                end
                frame_idx = frame_idx + 1

				local target = frame_idx * frame_time
				local elapsed = os.clock() - start
				local wait = target - elapsed
				if wait > 0 then
					sleep(wait)
				end
				os.pullEvent()
            end
        end

        if not looped or not saw_frame then break end
    end

    file.close()
end

-- ─────────────────────────────────────────────────────────────────────
-- Видео: сжатое (RLE)
-- ─────────────────────────────────────────────────────────────────────
function draw_decomp(filename, header)
    local file = fs.open(filename, "rb")
    if not file then print("ERR: cannot open "..filename); return end

    local width, height = header.width, header.height
    local fps, looped   = header.fps, header.isLooped
    local HEADER        = header.headerSize

    local bytes_per_row = math.floor(width / 2)
    local total_bytes = bytes_per_row * height
    local odd_width = (width % 2) == 1
	
	local frame_time = 1 / fps
	local start = os.clock()
	local frame_idx = 0
	
    while true do
        file.seek("set", HEADER)
        local saw_frame = false

        while true do
            local marker = file.read(1)
            if not marker then break end
            local mb = string.byte(marker)

            if mb == 0xFF then
                break
            elseif mb == 0xAD then
                saw_frame = true
                local buf = {}
                local produced = 0
                while produced < total_bytes do
                    local vch = file.read(1); if not vch then break end
                    local cch = file.read(1); if not cch then break end
                    local val = string.byte(vch)
                    local cnt = string.byte(cch)
                    for i = 1, cnt do
                        produced = produced + 1
                        buf[produced] = val
                    end
                end

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
                local target = frame_idx * frame_time
				local elapsed = os.clock() - start
				local wait = target - elapsed
				if wait > 0 then
					sleep(wait)
				end
				os.pullEvent()
            end
        end

        if not looped or not saw_frame then break end
    end

    file.close()
end

-- ─────────────────────────────────────────────────────────────────────
-- CLI
-- ─────────────────────────────────────────────────────────────────────
local args = {...}
if #args == 0 then
    print("=== MMIF Player (CC:Tweaked optimized) ===")
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