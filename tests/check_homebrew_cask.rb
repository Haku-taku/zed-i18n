# Run with: brew ruby -- tests/check_homebrew_cask.rb <cask.rb> <manifest.json>
# Loads casks under simulated systems without downloading or installing artifacts.
require "json"
require "open3"
require "cask/cask_loader"
require "simulate_system"

cask_path = Pathname(ARGV.fetch(0)).expand_path
manifest = JSON.parse(File.read(ARGV.fetch(1)))
assets = manifest.fetch("assets").select { |asset| asset["kind"] == "app" }
universal = assets.any? { |asset| asset["locale"].nil? }
locales = universal ? [nil] : assets.select { |asset| asset["platform"] == "macos" }
                                     .map { |asset| asset.fetch("locale") }.uniq.sort
raise "No cask locales in manifest" if locales.empty?

locales.each do |locale|
  [:macos, :linux].each do |os|
    { arm: "aarch64", intel: "x86_64" }.each do |arch, release_arch|
      Homebrew::SimulateSystem.with(os: os, arch: arch) do
        config = Cask::Config.new(explicit: locale ? { languages: [locale] } : {})
        cask = Cask::CaskLoader.load(cask_path, config: config)
        expected = assets.find do |asset|
          asset["platform"] == os.to_s && asset["arch"] == release_arch && asset["locale"] == locale
        end
        raise "Missing manifest asset: #{locale} #{os} #{release_arch}" unless expected
        raise "Wrong URL: #{cask.url}" unless cask.url.to_s == expected.fetch("download_url")
        raise "Wrong checksum: #{os} #{release_arch}" unless cask.sha256.to_s == expected.fetch("sha256")
        raise "Cask must support both operating systems" unless cask.supports_macos? && cask.supports_linux?

        artifact_types = cask.artifacts.map { |artifact| artifact.class.name }.sort
        if os == :macos
          raise "Incorrect macOS artifacts: #{artifact_types}" unless artifact_types == ["Cask::Artifact::App", "Cask::Artifact::Binary"]
          raise "macOS auto-update changed" unless cask.auto_updates
        else
          raise "Incorrect Linux artifacts: #{artifact_types}" unless artifact_types == ["Cask::Artifact::CommandWrapper"]
          raise "Linux updates must use Homebrew" if cask.auto_updates
          name, options = cask.artifacts.first.to_args
          raise "Incorrect Linux command name" unless name.to_s == "zed-i18n"
          content = options.fetch(:content)
          raise "Incorrect bundle path" unless content.include?("#{cask.staged_path}/zed.app/bin/zed")
          raise "Missing argument forwarding" unless content.include?('"$@"')
          raise "Missing update instruction" unless content.include?("brew upgrade --cask zed-i18n")
          raise "Missing uninstall instruction" unless content.include?("brew uninstall --cask zed-i18n")
          _, error, status = Open3.capture3("sh", "-n", stdin_data: content)
          raise "Invalid launcher shell syntax: #{error}" unless status.success?
        end
        puts "PASS #{locale || 'universal'} #{os}/#{release_arch}: #{expected.fetch('name')} (URL, SHA-256, artifacts)"
      end
    end
  end
end
