require 'sketchup'
require 'json'
require 'time'

module SU_MCP
  # What a pool instance shows while an MCP session (or the
  # sketchup-launcher CLI) has leased it, so the user knows not to touch it:
  # Claude's icon in the bottom left of the viewport, which names the lease
  # when hovered over, and a "Claude" toolbar with one button, which switches
  # to the session that holds it. Overlays get no clicks, hence the button.
  #
  # The lease is in the pool's state file, which the launcher names in
  # SKETCHUP_MCP_POOL_FILE when it starts a pool instance (see launcher.py in
  # the MCP server). Without it, as in a SketchUp started by hand, there's
  # neither. The file is read every POLL_INTERVAL seconds, and a lease counts
  # only while its holder process runs, so a session that died without
  # releasing doesn't leave them up.
  #
  # The icon is images/claude.svg, from Wikimedia Commons
  # (File:Claude-ai-icon.svg, CC0), and the PNGs beside it are that SVG
  # rendered at the badge's sizes, since overlays draw images, not SVG.
  module LeaseBadge
    POOL_FILE = ENV['SKETCHUP_MCP_POOL_FILE']
    POLL_INTERVAL = 2
    IMAGES = File.join(__dir__, 'images')

    @lease = nil
    @src = nil
    @failed = false

    class << self
      # The lease, as the pool's state file has it, or nil.
      attr_reader :lease
      # The source folder this instance was started with.
      attr_reader :src
    end

    def self.start
      return unless POOL_FILE

      if defined?(Sketchup::Overlay)
        attach(Sketchup.active_model)
        Sketchup.add_observer(AppObserver.new)
      end
      toolbar.hide if toolbar.visible?
      poll
      UI.start_timer(POLL_INTERVAL, true) { poll }
    end

    # Adds the overlay to a model; SketchUp gives each model its own.
    def self.attach(model)
      return unless model
      return if model.overlays.any? { |overlay| overlay.overlay_id == Overlay::ID }

      overlay = Overlay.new
      model.overlays.add(overlay)
      # Overlays start disabled (observed in SketchUp 2026; not documented).
      overlay.enabled = true
    rescue Exception => e
      warn "MCP: lease badge: #{e.class}: #{e.message}"
    end

    def self.poll
      lease, src = read
      return if lease == @lease && src == @src

      @lease = lease
      @src = src
      update_toolbar
      view = Sketchup.active_model&.active_view
      view&.invalidate
    rescue Exception => e
      # Once, not every two seconds.
      warn "MCP: lease badge: #{e.class}: #{e.message}" unless @failed
      @failed = true
    end

    # [lease or nil, source folder] for this instance: its record in the
    # pool's state file, found by port and checked to be this process.
    def self.read
      return [nil, nil] unless File.exist?(POOL_FILE)

      data = JSON.parse(File.read(POOL_FILE))
      record = data['instances'].find { |r| r['port'] == PORT && r['pid'] == Process.pid }
      return [nil, nil] unless record

      lease = record['lease']
      lease = nil unless lease && running?(lease['holder_pid'])
      [lease, (record['env'] || {})['SHELLIFY_SRC']]
    end

    def self.running?(pid)
      Process.kill(0, pid.to_i)
      true
    rescue Errno::ESRCH
      false
    rescue Errno::EPERM
      true
    end

    # Switches to the session that holds the lease, through the link the
    # MCP server recorded (a claude:// URL, which the Claude app opens).
    def self.open_session
      link = @lease && @lease['link']
      UI.openURL(link) if link
    end

    # Who holds the lease, and since when.
    def self.lines
      label = @lease['label'] && !@lease['label'].empty? ? @lease['label'] : 'unknown'
      since = begin
        Time.iso8601(@lease['leased_at']).localtime.strftime('%H:%M')
      rescue StandardError
        '?'
      end
      ["owner: \"#{label}\"", "since: #{since}"]
    end

    # The toolbar, shown only while leased. Its button is greyed out for a
    # lease with no session to open, such as one from the CLI.
    def self.toolbar
      @toolbar ||= begin
        @command = UI::Command.new('Claude') { LeaseBadge.open_session }
        icon = File.join(IMAGES, 'claude.svg')
        @command.small_icon = icon
        @command.large_icon = icon
        @command.tooltip = 'Claude'
        @command.set_validation_proc { @lease && @lease['link'] ? MF_ENABLED : MF_GRAYED }
        toolbar = UI::Toolbar.new('Claude')
        toolbar.add_item(@command)
        toolbar
      end
    end

    def self.update_toolbar
      toolbar
      if @lease
        label = @lease['label'] && !@lease['label'].empty? ? @lease['label'] : 'a session'
        @command.tooltip = "Claude: #{label}"
        @command.status_bar_text = @lease['link'] ? 'Open the Claude session that leased this SketchUp' : 'Leased from a shell, not a Claude session'
        toolbar.show unless toolbar.visible?
      elsif toolbar.visible?
        toolbar.hide
      end
    end

    class AppObserver < Sketchup::AppObserver
      def onNewModel(model)
        LeaseBadge.attach(model)
      end

      def onOpenModel(model)
        LeaseBadge.attach(model)
      end
    end

    if defined?(Sketchup::Overlay)
      class Overlay < Sketchup::Overlay
        ID = 'su_mcp.lease_badge'.freeze

        # In logical pixels, which draw2d and mouse events use from SketchUp
        # 2025 on.
        SIZE = 30
        MARGIN = 14
        LINE_HEIGHT = 16
        PADDING = 8
        TEXT_SIZE = 10

        # The icon rendered at SIZE times each scale factor.
        ICONS = { 1.0 => 'claude-30.png', 1.5 => 'claude-45.png', 2.0 => 'claude-60.png' }.freeze
        # Each corner's place in the image, top left first, going round as
        # the corners do. v runs up the image, as in OpenGL.
        UVS = [[0, 1, 0], [1, 1, 0], [1, 0, 0], [0, 0, 0]].freeze

        ORANGE = Sketchup::Color.new(215, 118, 85)
        LABEL = Sketchup::Color.new(38, 38, 36, 235)
        TEXT = Sketchup::Color.new(250, 249, 245)

        def initialize
          super(ID, 'Claude lease', description: 'Shows when an MCP session has leased this SketchUp')
          @hover = false
          @texture = nil
        end

        def draw(view)
          return unless LeaseBadge.lease

          @texture ||= load_icon(view)
          if @texture
            view.drawing_color = 'white'
            view.draw2d(GL_QUADS, corners(view), texture: @texture, uvs: UVS)
          else
            view.drawing_color = ORANGE
            view.draw2d(GL_QUADS, corners(view))
          end
          draw_label(view) if @hover
        rescue Exception => e
          warn "MCP: lease badge: #{e.class}: #{e.message}" unless @failed
          @failed = true
        end

        def onMouseMove(_flags, x, y, view)
          left, top = origin(view)
          hover = !LeaseBadge.lease.nil? &&
                  x.between?(left, left + SIZE) && y.between?(top, top + SIZE)
          return if hover == @hover

          @hover = hover
          view.invalidate
        end

        def onMouseLeave(view)
          return unless @hover

          @hover = false
          view.invalidate
        end

        private

        # The icon's top left, on whole pixels so it isn't blurred.
        def origin(view)
          [MARGIN, (view.vpheight - MARGIN - SIZE).round]
        end

        def corners(view)
          left, top = origin(view)
          [[left, top], [left + SIZE, top], [left + SIZE, top + SIZE], [left, top + SIZE]]
            .map { |x, y| Geom::Point3d.new(x, y, 0) }
        end

        # Once per overlay, not in every draw.
        def load_icon(view)
          scale = ICONS.keys.min_by { |s| (s - UI.scale_factor).abs }
          view.load_texture(Sketchup::ImageRep.new(File.join(IMAGES, ICONS[scale])))
        rescue Exception => e
          warn "MCP: lease badge: no icon: #{e.class}: #{e.message}"
          false
        end

        # Who holds the lease, in a box to the icon's right.
        def draw_label(view)
          lines = LeaseBadge.lines
          options = { size: TEXT_SIZE, color: TEXT }
          width = lines.map { |line| view.text_bounds(ORIGIN, line, options).width }.max
          left = MARGIN + SIZE + 6
          bottom = origin(view)[1] + SIZE
          top = bottom - lines.size * LINE_HEIGHT - PADDING * 2
          right = left + width + PADDING * 2
          view.drawing_color = LABEL
          view.draw2d(GL_QUADS, [[left, top], [right, top], [right, bottom], [left, bottom]]
            .map { |x, y| Geom::Point3d.new(x, y, 0) })
          lines.each_with_index do |line, i|
            view.draw_text(Geom::Point3d.new(left + PADDING, top + PADDING + i * LINE_HEIGHT, 0), line, options)
          end
        end
      end
    end
  end
end
