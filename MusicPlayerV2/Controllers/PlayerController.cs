using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Mvc;
using MusicPlayerV2.API;
using Microsoft.Extensions.Options;
using MusicPlayerV2.Shared;
using MusicPlayerV2.Domain;
using System.IO;

namespace MusicPlayerV2.Controllers
{


    [Route("api/player")]
    public class PlayerController : BaseController
    {
        private readonly IOptions<PlayerSettings> settings;
        private readonly LastFMManager lastFmManager;
        private readonly ListenBrainzManager listenbrainzManager;

        public PlayerController(IOptions<PlayerSettings> settings, LastFMManager lastFmManager, ListenBrainzManager listenbrainzManager)
        {
            this.settings = settings;
            this.lastFmManager = lastFmManager;
            this.listenbrainzManager = listenbrainzManager;
        }

        [HttpGet("GetCurrentPlayerState")]
        public GetPlayerStateResult GetCurrentPlayerState()
        {

            try
            {
                return GetPlayerStateResultFromPlayer<GetPlayerStateResult>();
            }
            catch (Exception ex)
            {
                return GetErrorResultFromException<GetPlayerStateResult>(ex);
            }
        }



        [HttpGet("GetCurrentTrackId")]
        public GetCurrentTrackIdResult GetCurrentTrackId(string oldTrackId = null)
        {
            try
            {
                var currentTrackId = Player.Instance.CurrentPlaylist.CurrentTrack == null ? "" : Player.Instance.CurrentPlaylist.CurrentTrack.Id + "";

                var hasChanged = oldTrackId + "" != currentTrackId;

                // long polling, don't respond to the request until the track has changed or timeout is reached
                DateTime cur = DateTime.UtcNow;
                while (!hasChanged && DateTime.UtcNow < cur.AddSeconds(30))
                {
                    System.Threading.Thread.Sleep(100);
                    currentTrackId = Player.Instance.CurrentPlaylist.CurrentTrack == null ? "" : Player.Instance.CurrentPlaylist.CurrentTrack.Id + "";
                    hasChanged = oldTrackId + "" != currentTrackId;
                }

                return new GetCurrentTrackIdResult()
                {
                    Success = true,
                    TrackId = currentTrackId
                };

            }
            catch (Exception ex)
            {
                return new GetCurrentTrackIdResult()
                {
                    Success = false,
                    Message = ex.GetType().FullName + " - " + ex.Message,
                };
            }
        }

        [HttpPost("PreviousTrack")]
        public PreviousTrackResult PreviousTrack([FromBody]PreviousTrackRequest request)
        {
            try
            {
                UpdateCurrentTrackPlayed(request.CurrentTrackId, false);

                Player.Instance.Previous();
                return GetPlayerStateResultFromPlayer<PreviousTrackResult>();
            }
            catch (Exception ex)
            {
                return GetErrorResultFromException<PreviousTrackResult>(ex);
            }
        }

        private void UpdateCurrentTrackPlayed(string currentTrackId, bool playedToEnd)
        {
            // todo update current track details before moving on
            using (DAL.DALManager mgr = new DAL.DALManager(settings.Value.DatabasePath))
            {
                var track = mgr.Get<Domain.Objects.Track>(int.Parse(currentTrackId));

                if (track == null)
                {// silently skip
                    Console.Error.WriteLine("Unable to update current track nr played because id '" + currentTrackId + "' was not found");
                    return;
                }

                if (track.Id == Player.Instance.CurrentPlaylist.CurrentTrack.Id && Player.Instance.CurrentPlaylist.CurrentTrackStartedPlaying)
                {
                    bool wasPlayedToEnd = playedToEnd;
                    if (playedToEnd)
                    {
                        // but is it really?, it's possible that it was just seeking to the end
                        var elapsedDuration = (DateTime.UtcNow - Player.Instance.CurrentPlaylist.CurrentTrackStartedPlayingOn).TotalSeconds;
                        if (track.Duration != 0 && elapsedDuration / track.Duration < 0.5)
                        {
                            // less than 50% of the track duration was passed, assume it was seeked to the end and don't update nr of played to the end
                            wasPlayedToEnd = false;
                        }
                    }

                    // only if the track actually started playing
                    track.NrPlayed++;

                    if (wasPlayedToEnd)
                        track.NrPlayedToEnd++;

                    // save a scrobble
                    Domain.Objects.Scrobble scrobble = new Domain.Objects.Scrobble()
                    {
                        TrackId = track.Id,
                        On = DateTime.UtcNow,
                        PlayedToEnd = wasPlayedToEnd
                    };
                    mgr.Set(scrobble);
                    // and notify last fm if played to end
                    if (wasPlayedToEnd)
                    {

                        var startedPlaying = DateTime.UtcNow.Subtract(TimeSpan.FromSeconds(track.Duration)); // Player.Instance.CurrentPlaylist.CurrentTrackStartedPlayingOn
                        lastFmManager.ScrobbleToLastFM(startedPlaying, track);
                        listenbrainzManager.Scrobble(startedPlaying, track);
                    }
                    mgr.Set(track);
                }
            }
        }

        [HttpPost("NextTrack")]
        public NextTrackResult NextTrack([FromBody]NextTrackRequest request)
        {
            try
            {
                UpdateCurrentTrackPlayed(request.CurrentTrackId, request.PlayedToEnd);

                Player.Instance.Next();
                Player.Instance.CurrentPlaylist.CurrentTrackStartedPlaying = false;
                Player.Instance.CurrentPlaylist.CurrentTrackStartedPlayingOn = DateTime.MinValue;
                return GetPlayerStateResultFromPlayer<NextTrackResult>();
            }
            catch (Exception ex)
            {
                return GetErrorResultFromException<NextTrackResult>(ex);
            }
        }

        [HttpPost("ToggleShuffle")]
        public ToggleShuffleResult ToggleShuffle([FromBody]ToggleShuffleRequest request)
        {
            try
            {
                if (request.Shuffle)
                    Player.Instance.Shuffle();
                else
                    Player.Instance.UnShuffle();

                return GetPlayerStateResultFromPlayer<ToggleShuffleResult>();
            }
            catch (Exception ex)
            {
                return GetErrorResultFromException<ToggleShuffleResult>(ex);
            }
        }

        [HttpPost("SetLikeStatus")]
        public SetLikeStatusRequestResult SetLikeStatus([FromBody]SetLikeStatusRequest request)
        {
            try
            {
                using (DAL.DALManager mgr = new DAL.DALManager(settings.Value.DatabasePath))
                {
                    var track = mgr.Get<Domain.Objects.Track>(int.Parse(request.TrackId));

                    if (track == null)
                        return new SetLikeStatusRequestResult() { Success = false, Message = "Track not found " };


                    var oldLikeStatus = track.LikeStatus;
                    var newLikeStatus = request.LikeStatus;
                    track.LikeStatus = request.LikeStatus;
                    mgr.Set(track);

                    // update last fm
                    try
                    {
                        if (oldLikeStatus != LikeStatus.Liked && newLikeStatus == LikeStatus.Liked)
                            lastFmManager.LoveTrackOnLastFM(Player.Instance.CurrentPlaylist.CurrentTrackStartedPlayingOn, track, true);
                        else if (oldLikeStatus == LikeStatus.Liked && newLikeStatus != LikeStatus.Liked)
                            lastFmManager.LoveTrackOnLastFM(Player.Instance.CurrentPlaylist.CurrentTrackStartedPlayingOn, track, false);
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine("Unable to update love status on Last FM: " + ex.Message);
                    }
                }

                return GetPlayerStateResultFromPlayer<SetLikeStatusRequestResult>();
            }
            catch (Exception ex)
            {
                return GetErrorResultFromException<SetLikeStatusRequestResult>(ex);
            }
        }

        [HttpPost("UpdatePlayerPlayingStatus")]
        public Result UpdatePlayerPlayingStatus([FromBody]UpdatePlayerPlayingStatusRequest request)
        {
            try
            {
                if (Player.Instance.CurrentPlaylist.CurrentTrack != null &&
                        Player.Instance.CurrentPlaylist.CurrentTrack.Id == int.Parse(request.TrackId))
                {

                    Domain.Objects.Track track;
                    using (DAL.DALManager mgr = new DAL.DALManager(settings.Value.DatabasePath))
                        track = mgr.Get<Domain.Objects.Track>(int.Parse(request.TrackId));

                    if (track == null)
                        return new Result() { Success = false, Message = "Track not found " };

                    if (request.IsPlaying)
                    {
                        // started playing
                        if (!Player.Instance.CurrentPlaylist.CurrentTrackStartedPlaying)
                        {
                            Player.Instance.CurrentPlaylist.CurrentTrackStartedPlaying = true;
                            Player.Instance.CurrentPlaylist.CurrentTrackStartedPlayingOn = DateTime.UtcNow;
                        }

                        lastFmManager.UpdateNowPlayingToLastFM(track);
                        listenbrainzManager.UpdateNowPlayingToListenBrainz(track);

                    }
                    else
                    {

                        // paused/stopped playing
                    }

                    return new Result()
                    {
                        Success = true
                    };
                }
                else
                {
                    return new Result()
                    {
                        Success = false,
                        Message = "Current track does not match specified track id"
                    };
                }
            }
            catch (Exception ex)
            {
                return GetErrorResultFromException<Result>(ex);
            }

        }

        [HttpPost("PlayAlbumOrTrackAfterCurrentTrack")]
        public Result PlayAlbumOrTrackAfterCurrentTrack([FromBody]PlayAlbumOrTrackRequest request)
        {

            // TODO
            return null;
        }


        [HttpPost("PlayAlbumOrTrackNow")]
        public PlayNowResult PlayAlbumOrTrackNow([FromBody]PlayAlbumOrTrackRequest request)
        {
            // TODO
            int playlistId;

            if (string.IsNullOrEmpty(request.PlaylistId))
                playlistId = Domain.Objects.Playlist.ALL_ID; // all
            else
                playlistId = int.Parse(request.PlaylistId);

            using (DAL.DALManager mgr = new DAL.DALManager(settings.Value.DatabasePath))
            {
                if (playlistId != Player.Instance.CurrentPlaylist.Id)
                {
                    PlayPlaylistNow(new PlayPlaylistRequest() { PlaylistId = playlistId + "" });
                }

                // find in the current playlist and change track

                if (request.Item.IsTrack)
                {
                    var track = mgr.Get<Domain.Objects.Track>(request.Item.Id);

                    if (track == null)
                        return new PlayNowResult() { Success = false, Message = "Track not found " };

                    Player.Instance.PlayTracks(new HashSet<int>() { request.Item.Id });
                }
                else
                {
                    var album = mgr.Get<Domain.Objects.Album>(request.Item.Id);
                    if (album == null)
                        return new PlayNowResult() { Success = false, Message = "Album not found" };

                    var tracks = mgr.FindTracks(null, playlistId, album.Id, 0, int.MaxValue);

                    Player.Instance.PlayTracks(new HashSet<int>(tracks.Select(t => t.Id)));
                }

            }

            return GetPlayerStateResultFromPlayer<PlayNowResult>();
        }

        [HttpPost("PlayPlaylistAfterCurrentTrack")]
        public Result PlayPlaylistAfterCurrentTrack([FromBody]PlayPlaylistRequest request)
        {
            // TODO
            return null;
        }

        [HttpPost("PlayPlaylistNow")]
        public GetPlayerStateResult PlayPlaylistNow([FromBody]PlayPlaylistRequest request)
        {
            if (request.PlaylistId == Player.Instance.CurrentPlaylist.Id + "")
                return new GetPlayerStateResult() { Success = false, Message = "Already playing this playlist" };

            using (DAL.DALManager mgr = new DAL.DALManager(settings.Value.DatabasePath))
            {
                if (request.PlaylistId == (Domain.Objects.Playlist.ALL_ID + "") || request.PlaylistId == (Domain.Objects.Playlist.LIKED_ID + ""))
                {
                    Player.Instance.LoadPlaylist(new Player.Playlist()
                    {
                        Id = int.Parse(request.PlaylistId), // all or liked so no fetching of playlist object
                        Tracks = mgr.GetTrackItems(int.Parse(request.PlaylistId)),
                    });
                }
                else
                {
                    var playlist = mgr.Get<Domain.Objects.Playlist>(int.Parse(request.PlaylistId));

                    if (playlist == null)
                        return new GetPlayerStateResult() { Success = false, Message = "Playlist not found" };

                    Player.Instance.LoadPlaylist(new Player.Playlist()
                    {
                        Id = playlist.Id,
                        Tracks = mgr.GetTrackItems(playlist.Id),
                    });
                }
            }
            return GetPlayerStateResultFromPlayer<GetPlayerStateResult>();
        }

        [HttpGet("GetDetails")]
        public GetDetailsResult GetDetails(GetDetailsRequest request)
        {
            try
            {
                Domain.Objects.Track track;
                List<Domain.Objects.Scrobble> scrobbles;
                using (DAL.DALManager mgr = new DAL.DALManager(settings.Value.DatabasePath))
                {
                    track = mgr.Get<Domain.Objects.Track>(int.Parse(request.TrackId));
                    scrobbles = mgr.GetScrobblesOfTrack(track.Id);
                }

                if (track == null)
                    return new GetDetailsResult() { Success = false, Message = "Track not found" };

                return new GetDetailsResult()
                {
                    Success = true,
                    Details = new TrackDetails()
                    {
                        Id = track.Id + "",
                        Title = track.Title,
                        Album = track.Album,
                        Artists = track.Artists?.Split(','),
                        Genres = System.Text.RegularExpressions.Regex.Split(track.Genres + "", "[,/]"),
                        TrackNr = track.TrackNr,
                        AddedOn = track.AddedOn,
                        LastPlayed = track.LastPlayed == DateTime.MinValue ? null : (DateTime?)track.LastPlayed,
                        Liked = track.LikeStatus,
                        NrPlayed = track.NrPlayed,
                        NrPlayedToEnd = track.NrPlayedToEnd,
                        LastScrobbles = scrobbles.OrderByDescending(s => s.On).Select(s => new TrackScrobble()
                        {
                            On = s.On,
                            PlayedToEnd = s.PlayedToEnd
                        }).ToArray()
                    }
                };
            }
            catch (Exception ex)
            {
                return GetErrorResultFromException<GetDetailsResult>(ex);
            }
        }

        [HttpGet("GetArt")]
        public IActionResult GetArt(int trackId)
        {
            if (trackId == 0)
                return NotFound();

            Domain.Objects.Track track;
            using (DAL.DALManager mgr = new DAL.DALManager(settings.Value.DatabasePath))
                track = mgr.Get<Domain.Objects.Track>(trackId);

            if (track == null)
                return NotFound();

            string path = System.IO.Path.Combine(settings.Value.RootDirectory, track.Filename);

            using (var tlfile = TagLib.File.Create(path))
            {
                TagLib.Tag tag123 = tlfile.Tag;

                if (tag123.Pictures != null && tag123.Pictures.Length > 0)
                {
                    return new FileContentResult(tag123.Pictures[0].Data.ToArray(), tag123.Pictures[0].MimeType);
                }
                else
                {
                    // check if cover file exists
                    string dir = System.IO.Path.GetDirectoryName(path);
                    string coverpath = System.IO.Path.Combine(dir, "cover.jpg");
                    string mimetype = "image/jpg";
                    if (!System.IO.File.Exists(coverpath))
                    {
                        coverpath = System.IO.Path.Combine(dir, "cover.png");
                        mimetype = "image/png";
                    }

                    if (System.IO.File.Exists(coverpath))
                    {
                        return new FileContentResult(System.IO.File.ReadAllBytes(coverpath), mimetype);
                    }
                    else
                        return NotFound();
                }
            }
        }

        [HttpGet("GetAudio")]
        public IActionResult GetAudio(int trackId)
        {
            if (trackId == 0)
                return NotFound();

            Domain.Objects.Track track;
            using (DAL.DALManager mgr = new DAL.DALManager(settings.Value.DatabasePath))
                track = mgr.Get<Domain.Objects.Track>(trackId);

            if (track == null)
                return NotFound();

            string filePath = System.IO.Path.Combine(settings.Value.RootDirectory, track.Filename);

            FileInfo fi = new FileInfo(filePath);

            if (!fi.Exists)
                return NotFound();

            Response.Headers["X-Content-Duration"] = track.Duration.ToString(System.Globalization.CultureInfo.InvariantCulture);

            DateTimeOffset lastModified = new DateTimeOffset(fi.LastWriteTimeUtc, TimeSpan.Zero);
            var entityTag = new Microsoft.Net.Http.Headers.EntityTagHeaderValue(
                '"' + fi.LastWriteTimeUtc.Ticks.ToString("x") + "-" + fi.Length.ToString("x") + '"');

            // PhysicalFile with enableRangeProcessing handles Range/If-Range/If-Modified-Since,
            // 206 Partial Content, Content-Range and Accept-Ranges headers for us, which Safari/WebKit
            // requires in order to seek within the audio stream instead of always getting a full 200 response.
            return PhysicalFile(filePath, "audio/mpeg", lastModified, entityTag, enableRangeProcessing: true);
        }

        private T GetPlayerStateResultFromPlayer<T>()
            where T : GetPlayerStateResult, new()
        {


            Domain.Objects.Track track;
            using (DAL.DALManager mgr = new DAL.DALManager(settings.Value.DatabasePath))
                track = Player.Instance.CurrentPlaylist.CurrentTrack == null ? null : mgr.Get<Domain.Objects.Track>(Player.Instance.CurrentPlaylist.CurrentTrack.Id);

            return new T()
            {
                Player = new PlayerState()
                {
                    CurrentTrack = track == null ? null : new Track()
                    {
                        Id = track.Id + "",
                        Title = track.Title,
                        Artists = track.Artists?.Split(","),
                        Album = track.Album,
                        TrackNr = track.TrackNr,
                        Liked = track.LikeStatus,

                        Url = Url.Action("GetAudio", new { trackId = track.Id }),
                        ArtImage = Url.Action("GetArt", new { trackId = track.Id })
                    },

                    Shuffle = Player.Instance.CurrentPlaylist.IsShuffled
                },

                Success = true
            };
        }
    }
}
