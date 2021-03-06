import discord
from discord.ext import commands
import aiohttp
import json
import pandas as pd
import datetime
import gc
import asyncio
import argparse
import shlex
import pickle
import io
import logging
from contextlib import suppress

# Store a constant dict of string->string for descriptions of 
# things like score adjustment events
status_map = dict(mask='Score mask',
                  delta='Elo delta',
                  set='Elo set')

class EloError(Exception):
    '''An error for Elo rating meant to be presented to the user nicely'''

    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


def valid_datetime(s):
    '''Try to parse a given timestamp as a datetime
    object. Only accept a timestamp in the format [YYYY-]mm-dd hh:mm.
    '''

    if s != str(s):
        s = '/'.join(s)
    else:
        pass


    timestamp = None

    try:
        # Full date and full time
        timestamp = datetime.datetime.strptime(s, '%Y-%m-%d/%H:%M')
        return timestamp
    except ValueError:
        pass
    # The same without the year attached
    try:
        # Date and time but year ommited: fill in with current year
        timestamp = datetime.datetime.strptime(s, '%m-%d/%H:%M')
        timestamp = timestamp.replace(year=datetime.date.today().year)
        return timestamp
    except ValueError:
        pass

    # If it doesn't work, then we've gotta complain instead of silently failing!
    # So if all those methods failed, we complain
    if timestamp is None:
        # Complain here!
        raise argparse.ArgumentTypeError('Invalid timestamp format! '
                '(Valid formats are [YYYY-]mm-dd hh:mm in 24-hour UTC time.)')

async def has_player_perms(ctx):
    # Return True if the caller has permission
    # to run queries

    # If the player is an admin, return true. (admin perms are a superset of players')
    if await has_admin_perms(ctx):
        return True
    # If the player is not an admin, but needs admin permission, then return false
    if ctx.bot.elo_config['queries_need_admin_perms']:
        return False
    # If the player needs a special role, then check for that role
    if ctx.bot.elo_config['player_perms_need_role']:
        for role in ctx.message.author.roles:
            if ctx.bot.elo_config['player_role_name'] == role.name:
                return True

        return False
    # Otherwise, return true
    return True

async def has_admin_perms(ctx):
    # Return True of the caller has permission
    # to add, mutate, delete...

    for role in ctx.message.author.roles:
        if ctx.bot.elo_config['admin_role_name'] == role.name:
            return True
    return False


class EloEventConverter(commands.Converter):

    # Set up a subclass of ArgumentParser so we can get errors...
    class EloArgumentParser(argparse.ArgumentParser):

        def error(self, message):
            raise EloError(message)

    # Set up argument parsers
    match_parser = EloArgumentParser(prog='')
    match_parser.add_argument('match', nargs='+')
    match_parser.add_argument('--k-factor', '-k', type=int)
    match_parser.add_argument('--comment', '-c')
    match_parser.add_argument('--timestamp', '-t', type=valid_datetime)

    adjustment_parser = EloArgumentParser(prog='')
    adjustment_parser.add_argument('player')
    adjustment_parser.add_argument('value', type=int)
    adjustment_parser.add_argument('--comment', '-c')
    adjustment_parser.add_argument('--timestamp', '-t', type=valid_datetime)

    def __init__(self):

        super().__init__()

        # There should be a dict here mapping event_type to functions.
        self.event_parser_map = {'match': self.parse_match,
                            'mask': self.parse_adjustment,
                            'delta': self.parse_adjustment,
                            'set': self.parse_adjustment}


    async def parse_user(self, ctx, user_spec):
        '''Convenience function for parsing users in mention,
        id, or name format.

        Requires the invocation context to call Guild
        methods.
        '''

        # Try to see if there's a user with this name already.
        member = ctx.guild.get_member_named(user_spec)
        if member is not None:
            return member

        # Try to parse the user's ID if it's directly
        # specified in chat...
        try:
            user_id = int(user_spec.strip('<!@>'))
        except ValueError:
            pass
        else:
            member = ctx.guild.get_member(user_id)
            if member is not None:
                return member

        return None



    async def parse_match(self, ctx, event_type, event_spec):
        # Here, we should parse matches...

        # Have our handy parser parse everything but the teams...
        args = self.match_parser.parse_args(shlex.split(event_spec))

        # Manually parse teams
        teams = []
        team = []
        team_number = 0
        for elem in args.match:
            if elem in self.config['status_values']:
                # This means we're done with this team!
                for player in team:
                    teams.append((player, team_number, elem))
                team = []
                team_number += 1
            else:
                user = await self.parse_user(ctx, elem)
                if user is None:
                    raise EloError("Couldn't parse user `{}`!".format(elem))
                team.append(user.id)

        if team_number < 2:
            # We got less than 2 teams! This doesn't make sense!!
            raise EloError("Need at least 2 teams for a match!")

        if team_number > self.config['max_teams']:
            # We got too many teams!
            raise EloError("Too many teams in this match!")

        # Now we can make the dataframe...
        match_df = pd.DataFrame(teams, columns=['playerID', 'team', 'status'])
        match_df['value'] = args.k_factor if args.k_factor is not None else None
        match_df['comment'] = args.comment
        match_df['timestamp'] = args.timestamp

        # Disallow duplicate players
        if match_df['playerID'].duplicated().any():
            raise EloError('Players must be unique!')

        return match_df

    async def parse_adjustment(self, ctx, event_type, event_spec):
        # Parse single player events
        args = self.adjustment_parser.parse_args(shlex.split(event_spec))

        member = await self.parse_user(ctx, args.player)

        event_df = pd.DataFrame([[member.id, 0, event_type, args.value,
                                 args.comment, args.timestamp]],
                                columns=['playerID', 'team', 'status',
                                         'value', 'comment', 'timestamp'])
        return event_df

    async def convert(self, ctx, argument):

        # The first word should be the type of event.
        # Parse that out, and determine how we want to parse 
        # the rest of the event string accordingly.

        self.config = ctx.bot.elo_config

        try: 
            event_type, event_spec = argument.split(maxsplit=1)
        except ValueError:
            raise EloError('Wrong event format! Events must be specified as `EVENTTYPE EVENTSPEC`')

        if event_type not in self.event_parser_map:
            raise EloError('Unknown event type `{}`!\n'.format(event_type)
                           + 'Try one of `' + '`, `'.join(self.event_parser_map.keys())
                           + '`!')
        return await self.event_parser_map[event_type](ctx, event_type, event_spec)

async def on_command_error(ctx, error):
    '''Global error handler which gives EloError back to the user'''

    # print('Caught error of type {}!'.format(type(error)))

    if hasattr(error, 'original'):
        original = error.original

        if isinstance(original, EloError):
            await ctx.message.channel.send(original)
        else:
            raise original
    elif isinstance(error, EloError):
        await ctx.message.channel.send(error);
    elif isinstance(error, commands.errors.CheckFailure):
        await ctx.message.channel.send("You don't have permission to execute this command!")
    elif isinstance(error, commands.errors.CommandNotFound):
        await ctx.message.channel.send("Unknown command! Type `elo help` for help.")
    elif isinstance(error, commands.errors.MissingRequiredArgument):
        await ctx.message.channel.send("Missing required argument: {}".format(error))
    elif isinstance(error, commands.errors.BadArgument):
        if hasattr(error, '__cause__'):
            original = error.__cause__
            if isinstance(original, EloError):
                await ctx.message.channel.send(original)
            elif isinstance(original, ValueError):
                if 'invalid literal for int' in str(original):
                    val = str(original)
                    val = val[val.find("'")+1:val.rfind("'")]
                    await ctx.message.channel.send('Was expecting integer but got `{}` instead!'.format(val))
            else:
                raise original
        else:
            raise error
    else:
        raise error

class Elo:
    '''
    Elo rating commands from Elo-sensei
    '''

    def __init__(self, bot, config):
        self.bot = bot
        self.config = config['elo']
        self.logger = logging.getLogger('elo')

        # We also need to load the dataframes for both the match history
        # and current users status, given paths in the config
        try:
            self.match_history = pd.read_pickle(self.config['match_history_path'])
        except OSError:
            # Create a new match history
            self.match_history = pd.DataFrame(columns=['timestamp', 'eventID', 'playerID', 'elo', 'new_elo', 'team', 'status', 'value', 'comment'])

        try:
            self.user_status = pd.read_pickle(self.config['user_status_path'])
        except OSError:
            # Create new user status
            self.user_status = pd.DataFrame(columns=['name', 'elo', 'wins', 'losses', 'matches_played', 'rank', 'color', 'mask'])
            self.user_status.index.name = 'playerID'

            # Set categorical dtype for rank
            self.user_status['rank'] = self.user_status['rank'].astype('category')

            # Get all the possible ranks and add them to the categorical type
            all_ranks = [rank['name'] for rank in self.config['ranks']]
            self.user_status['rank'] = self.user_status['rank'].cat.add_categories(all_ranks)

        # Create locks to prevent race conditions during asynchronous operation
        self.user_status_lock = asyncio.Lock()
        self.match_history_lock = asyncio.Lock()

        bot.elo_config = self.config

        # Create parser for users
        self.parser = EloEventConverter()

        # Handle command errors...
        bot.on_command_error = on_command_error

        # Begin periodic task to save df's
        if self.config['periodic_save']:
            self.logger.info('Periodic save enabled, with period {} seconds.'.format(self.config['periodic_save_interval']))
            self.periodic_save_task = asyncio.ensure_future(self.periodic_save())
        else:
            self.logger.info('Periodic save disabled.')

    async def periodic_save(self):
        '''Periodically save the dataframes,
        using asyncio.sleep.
        '''

        while True:
            await asyncio.sleep(self.config['periodic_save_interval'])

            # Suppress CancelledError because we do not want to die
            # in the middle of saving.
            # But when it's done saving, if it's saving at all,
            # the Task will exit.
            with suppress(asyncio.CancelledError):
                self.logger.info('Autosaving event and user data.')
                await self.save_dataframes()

    async def do_shutdown_tasks(self):

        if self.config['save_on_shutdown']:
            self.logger.info('Saving event and user data before shutdown...')
            await self.save_dataframes()
            self.logger.info('Successfully saved.')

    async def save_dataframes(self, use_locks=True):
        '''Save all dataframes to disk.
        Note that this coroutine function acquires and releases locks itself
        by default!'''

        if use_locks:
            await self.acquire_locks()
        self.user_status.to_pickle(self.config['user_status_path'])
        self.match_history.to_pickle(self.config['match_history_path'])
        if use_locks:
            self.release_locks()


    async def acquire_locks(self):
        '''Acquire locks for dataframes'''

        await self.match_history_lock.acquire()
        await self.user_status_lock.acquire()

    def release_locks(self):
        '''Release locks for dataframes'''

        self.match_history_lock.release()
        self.user_status_lock.release()

    def raise_error(self, message='Internal error!'):
        '''Release locks and raise an EloError with the given message.'''

        self.release_locks()
        raise EloError(message)

    def get_elo(self, user_status, player):
        '''Get the raw Elo rating from the given user status df.
        If the playerID given is not in the df, add a corresponding row.'''

        if player in user_status.index:
            return user_status.loc[player, 'elo']
        else:
            # print(user_status)
            user_status.loc[player] = dict(name=None, elo=self.config['default_elo'],
                    wins=0, losses=0, matches_played=0, rank=None, color=None, mask=0)
                    
            return self.config['default_elo']


    def get_masked_elo(self, user_status, player):
        '''Get the masked Elo rating from the given user status df.
        Calls get_elo, then adds the mask.'''

        raw_elo = self.get_elo(user_status, player)
        return raw_elo + user_status.loc[player, 'mask']

    async def recalculate_elo(self, ctx):
        '''Recalculate the Elo ratings and masks from scratch.'''
        
        # Reinstantiate user status
        user_status = pd.DataFrame(columns=self.user_status.columns) # Columns: [name, elo, wins, losses, matches_played, rank, color, mask]
        user_status.index.name = 'playerID'
        
        # Reinstantiate match history
        match_history = pd.DataFrame(columns=self.match_history.columns) # Columns: [comment, elo, eventID, new_elo, playerID, status, team, timestamp, value]

        # For each match...
        hist = self.match_history.sort_values('timestamp', ascending=False)

        
        for time, match in hist.groupby('timestamp', as_index=False):
            
            # time is the grouping label, match is a dataframe with the data for that group    

            match = match.copy() 

            # Replace the elo rating of each player with what it should be, from the user_status
            match['elo'] = match['playerID'].apply(lambda p: self.get_elo(user_status, p))

            # Update the user status for each player
            user_status = await self.update_players(ctx, match, user_status, update_roles=False)

            # Grab the new elo
            match = match.drop('new_elo', axis=1, errors='ignore')
            match['playerID']=match['playerID'].apply(int) 
            match = match.merge(user_status.reset_index()[['playerID', 'elo']].rename(columns=dict(elo='new_elo')), on='playerID')

            # Add the match to the new match history
            match_history = match_history.append(match, ignore_index=True)


        # Finally, update the Elo object's match history and user status
        self.match_history = match_history
        self.user_status = user_status

        # Update nicks...
        players = user_status.index.tolist()
        await self.update_nicks(ctx, players)

        # Update roles
        # DEBUG
        init_time = datetime.datetime.now()
        # END DEBUG
        for uid in players:
            await self.update_rank(ctx, user_status, uid, update_roles=True)
        print('Processing ranks took: {}'.format(datetime.datetime.now() - init_time))


    async def process_single_player_events(self, ctx, match_df, user_status, lock=None, update_roles=True):

        event = match_df.iloc[0]
        if lock is not None:
            await lock.acquire()
        elo = self.get_elo(user_status, event['playerID'])
        if event['status'] == 'delta':
            user_status.loc[event['playerID'], 'elo'] += event['value']
        elif event['status'] == 'set':
            user_status.loc[event['playerID'], 'elo'] = event['value']
        elif event['status'] == 'mask':
            user_status.loc[event['playerID'], 'mask'] += event['value']

        await self.update_rank(ctx, user_status, event['playerID'], update_roles=update_roles)
        if lock is not None:
            lock.release()

        return user_status


    async def update_players(self, ctx, match_df, user_status, lock=None, update_roles=True):
        '''Update the given user_status df using the event described in match_df.
        If update_roles is True, update the users' Discord roles as well.
        if lock is not None, acquire and release that lock to access user_status.'''

        # If this isn't a match, process it as a single player event (e.g. score adjustment)
        if len(match_df) == 1:
            return await self.process_single_player_events(ctx, match_df, user_status, lock=lock, update_roles=update_roles)

        # Otherwise, continue to process it as a normal match
        team_elo = match_df.groupby('team')[['elo']].sum()

        # Bring in the first values of status and value for each team
        team_elo[['status', 'value']] = match_df.groupby('team').head(1).set_index('team')[['status', 'value']]

        if lock is not None:
            await lock.acquire()
        user_status = user_status.copy()
        if lock is not None:
            lock.release()

        # The following is the MEAT of the Elo rating calculation

        # Take mean of every team but this one
        for index, row in team_elo.iterrows():
            team_elo.loc[index, 'other_elo'] = team_elo.drop(index)['elo'].mean()
        
        # Expected score for teams
        # This uses the logistic curve and formulas from Wikipedia
        team_elo['expected'] = 1./(1.+10.**((team_elo['other_elo'] - team_elo['elo'])/400))

        # Actual team scores
        team_elo['actual'] = team_elo['status'].apply(self.get_status_value)

        # If allowing only defined status values, we might have NaN values in there...
        # Fail if that happens..
        if team_elo['actual'].isnull().any():
            self.raise_error('Unknown team status! Try one of `'+
                               ('`, `').join(self.config['status_values'].keys()) + '`!')

        # If score limit must be met exactly...
        if self.config['require_score_limit'] and team_elo['actual'].sum() != self.config['score_limit']:
            self.raise_error('Not enough/too many teams are winning/losing!')

        # Limit total score
        if team_elo['actual'].sum() > self.config['score_limit']:
            self.release_locks()
            self.raise_error('Maximum score exceeded! Make sure the teams are not all winning!')

        # Main formula for elo delta - the value column has the K factor.
        # Multiplication and subtraction here are POINTWISE.
        team_elo['elo_delta'] = team_elo['value'] * (team_elo['actual'] - team_elo['expected'])


        for index, row in match_df.iterrows():

            # Update users in user_status given their elo_deltas
            player = row['playerID']
            user_status.loc[player, 'elo'] += team_elo.loc[row.team, 'elo_delta']
            actual_score = team_elo.loc[row.team, 'actual']
            user_status.loc[player, 'matches_played'] += 1
            if actual_score == 1:
                user_status.loc[player, 'wins'] += 1
            elif actual_score == 0:
                user_status.loc[player, 'losses'] += 1
            await self.update_rank(ctx, user_status, row['playerID'], update_roles=update_roles)

        return user_status

    async def update_rank(self, ctx, user_status, uid, update_roles=True):
        '''Update the rank of player with user ID uid in user_status.'''

        # Pick which way we want to get the elo...
        if self.config['ranks_use_raw_elo']:
            elo = self.get_elo
        else:
            elo = self.get_masked_elo

        # Figure out which rank we are to give this user
        max_rank = None
        for rank in self.config['ranks']:
            if max_rank is None:
                if elo(user_status, uid) > rank['cutoff'] or rank['default']:
                    max_rank = rank
            else:
                if elo(user_status, uid) > rank['cutoff'] and rank['cutoff'] > max_rank['cutoff']:
                    max_rank = rank

        # Actually give the rank
        user_status.loc[uid, 'rank'] = max_rank['name']
        user_status.loc[uid, 'color'] = max_rank['color']

        # Give discord role corresponding to rank, and remove old one, if any.
        # Check if we have the permission to manipulate discord roles
        if ctx.guild.me.permissions_in(ctx.message.channel).manage_roles and update_roles:
            # We are allowed to manipulate roles
            # Get all rank names
            all_ranks = [r['name'] for r in self.config['ranks']]

            # Remove current role by name
            member = ctx.guild.get_member(uid)
            roles = member.roles
            roles = [r for r in roles if r.name not in all_ranks]

            # Find desired role by name
            for role in ctx.guild.roles:
                if role.name == max_rank['name']:
                    # We found the right role
                    roles.append(role)

            # Edit user to change roles
            try:
                await member.edit(roles=roles)
            except discord.errors.Forbidden:
                pass


    def get_status_value(self, status):
        '''Parse status value (win, loss, draw) for matches'''
        try:
            return self.config['status_values'][status]
        except:
            if self.config['allow_only_defined_status_values']:
                # This will become NaN in the dataframe
                # and we can catch it later
                return None

    @commands.command()
    @commands.check(has_admin_perms)
    async def save(self, ctx):
        '''Force the bot to save the dataframes.'''

        self.logger.info('Manual save of events and users started by {} ({}).'
                .format(ctx.message.author.name, ctx.message.author.id))
        await self.save_dataframes()
        await ctx.message.channel.send('Save successful!')


    @commands.command()
    @commands.check(has_admin_perms)
    async def add(self, ctx, *, event: EloEventConverter()):
        '''Add an event to the match history.

        USAGE: add EVENT

        For specific help, see the documentation at 
        https://github.com/lekro/elosensei/wiki/Manipulating-events
        '''
        # Get locks
        await self.acquire_locks()

        # We should have already gotten a dataframe as event,
        # since we wrote EloEventConverter, which should have
        # already parsed the event...

        # Fill in current time if timestamp was not specified.
        event['timestamp'] = event['timestamp'].fillna(datetime.datetime.utcnow())
                
        # Fill in current elo of players...
        event['elo'] = event['playerID'].map(lambda x: self.get_elo(self.user_status, x))
        event['value'] = event['value'].fillna(self.config['k_factor'])

        # Now we're ready to update the players' Elo ratings...
        new_user_status = await self.update_players(ctx, event, self.user_status)

        # If the update was successful, update the Elo ratings of all players
        if new_user_status is not None:
            self.user_status = new_user_status
        else:
            return

        # Bring the new elo scores back into the match dataframe...
        event = event.merge(self.user_status.reset_index()[['playerID', 'elo']]
                .rename(columns=dict(elo='new_elo')), on='playerID')


        # Assign an eventID
        if len(self.match_history > 0):
            event['eventID'] = self.match_history['eventID'].max() + 1
        else: 
            event['eventID'] = self.match_history['timestamp'].nunique() + 1

        # Update nicks
        players = event['playerID'].tolist()
        await self.update_nicks(ctx, players)
        
        # Add this event to the match history
        self.match_history = self.match_history.append(event, ignore_index=True)
        # Sometimes there are circular references within dataframes? so we have to
        # invoke the gc
        gc.collect()
        timestamp = event.timestamp.iloc[0]

        # In case the timestamp was older than the latest event, we need to recalculate
        # elo! This event belongs somewhere in the middle of the match history,
        # in that case.
        if timestamp < self.match_history['timestamp'].max():
            print('Timestamp {} was older than latest {}!'.format(timestamp,
                self.match_history['timestamp'].max()))
            await self.recalculate_elo(ctx)
        await ctx.message.channel.send(embed=await self.get_event_embed(ctx, timestamp))

        # Release locks
        self.release_locks()

    
    async def update_nicks(self, ctx, uids):
        '''Update nicks/names in the user_status df.'''
        
        # Update user nicks

        for user_id in uids:
            try:
                member = ctx.guild.get_member(user_id)
            except ValueError:
                pass
            else:
                self.user_status.loc[member.id, 'name'] = member.display_name



    @commands.command()
    @commands.check(has_admin_perms)
    async def edit(self, ctx, eventid: int, *, event: EloEventConverter()):
        '''Edit an event.

        USAGE: edit EVENTID EVENT

        For specific help, read the documentation at
        https://github.com/lekro/elosensei/wiki/Manipulating-events
        '''

        await self.acquire_locks()

        # Check if this event exists
        if eventid not in self.match_history['eventID'].tolist():
            raise_error("Can't edit a nonexisting event!")
        old_event = self.match_history.query('eventID == @eventid')

        # Fill in NA values from old event.
        event['value'] = event['value'].fillna(old_event['value'].iloc[0])
        if old_event['comment'].iloc[0] is not None:
            event['comment'] = event['comment'].fillna(old_event['comment'].iloc[0])
        event['timestamp'] = event['timestamp'].fillna(old_event['timestamp'].iloc[0])

        # Put old eventID
        event['eventID'] = old_event['eventID'].iloc[0]
        new_history = self.match_history.query('eventID != @eventid').append(event,
                ignore_index=True)

        self.match_history = new_history

        await self.recalculate_elo(ctx)

        self.release_locks()

        await ctx.message.channel.send('Edited event!', 
                embed=await self.get_event_embed(ctx, event['timestamp'].iloc[0]))
    
    @commands.command()
    @commands.check(has_admin_perms)
    async def delete(self, ctx, eventid: int):
        '''Delete an event.

        USAGE: delete EVENTID

        Deletes the event with ID EVENTID.
        '''

        init_time = datetime.datetime.now()
        await self.acquire_locks()

        # Check if this event exists
        if eventid not in self.match_history['eventID'].tolist():
            self.raise_error("Can't delete a nonexisting event!")
        self.match_history = self.match_history.query('eventID != @eventid')
        
        await self.recalculate_elo(ctx)
        print('time taken for delete: {}'.format(datetime.datetime.now() - init_time))

        self.release_locks()

        await ctx.message.channel.send('Deleted event!')
        


    @commands.command()
    @commands.check(has_admin_perms)
    async def backup(self, ctx, name: str):
        '''Request that the bot upload a Python pickle of the 
        specified dataframe.

        USAGE: backup NAME
        where NAME is the name of the dataframe to back up.

        Note that the output format is a Python pickle. If you 
        wish to manipulate the output, you should open it up in
        python using pandas:

        import pandas as pd
        df = pd.read_pickle('/path/to/backup.pickle')
        # Do what you need to with the df
        '''

        # Map args -> df
        backup_map = {'users': self.user_status,
                      'events': self.match_history}

        # Fail if the user picked some arg we don't know about
        if name not in backup_map:
            await ctx.message.channel.send("Unknown dataframe name! Try one of `"
                                     + "`, `".join(backup_map.keys())
                                     + "`!")
            return

        await self.acquire_locks()

        df = backup_map[name]
        # The bot is only compatible with Python>=3.5, so it doesn't
        # hurt to use a pickle protocol available in Python>=3.4.
        # We are converting to bytes with pickle instead of using
        # pandas.to_pickle so we can keep all operations in memory
        # and not write to disk at all.
        backup_bytes = pickle.dumps(df, protocol=-1)

        # Release locks now. Now all we need to do is send the backup_bytes to discord.
        self.release_locks()

        # Get BytesIO object reading from backup_bytes
        backup_io = io.BytesIO(backup_bytes)

        # Create a discord File reading from that BytesIO
        fi = discord.File(backup_io, filename='{}.pickle'.format(name))

        # Send it
        await ctx.message.channel.send('Backup of `{}`, made on {}'.format(name, datetime.datetime.utcnow()),
                                       file=fi)

    @commands.command()
    @commands.check(has_player_perms)
    async def show(self, ctx, *, arg):
        '''Display information for a match or event given a date or event ID.

        show [match-id-or-time] [page]

        Display event with ID #14: show 14
        Display all events on 2017-01-01: show 2017-01-01
        Display the second page of events on 2017-01-01: show 2017-01-01 2
        '''

        await self.acquire_locks()

        if len(self.match_history) < 1:
            self.raise_error("No events have been added!")

        # We will have one or two arguments...
        args = arg.split()
        if len(args) > 1:
            try:
                page = int(args[1])-1
                print('page requested: %d' % page)
            except ValueError:
                self.raise_error("Page number must be an integer!")
        else:
            page = 0
        arg = args[0]

        try:
            eventID = int(arg)
        except ValueError:
            # Try to parse it as a date
            try:
                timestamp = datetime.datetime.strptime(arg, '%Y-%m-%d')
            except ValueError:
                raise EloError("Couldn't parse argument as event ID or date!")
            else:
                mask = (timestamp <= self.match_history['timestamp']) & \
                        (timestamp + datetime.timedelta(days=1) > self.match_history['timestamp'])
                timestamps = self.match_history.drop_duplicates(subset='timestamp').loc[mask, 'timestamp'].dt.to_pydatetime()
                del mask
        else:
            if eventID not in self.match_history['eventID'].tolist():
                self.raise_error("Couldn't find an event with ID #{}!".format(eventID))
            mask = self.match_history['eventID'] == eventID
            timestamps = self.match_history.drop_duplicates(subset='timestamp').loc[mask, 'timestamp'].dt.to_pydatetime()

        if len(timestamps) < 1:
            self.raise_error("No events found!")

        print(timestamps)

        event_cards = [await self.get_event_embed(ctx, ts) for ts in timestamps]

        page_size = self.config['max_match_cards']

        # If we find only one page of players, just output them.
        if len(event_cards) <= page_size:
            for card in event_cards:
                await ctx.message.channel.send(embed=card)
        # If we find more than one page, show the page number as well
        else:
            page_count = (len(event_cards) + page_size - 1) / page_size
            if not (0 <= page < page_count):
                self.release_locks()
                raise EloError("Page index out of range!")
            # Iterate through the player cards only in the page we want...
            for i, card in enumerate(event_cards[page*page_size:(page+1)*page_size]):
                if i==0:
                    page_string = 'Showing page %d of %d of event cards.' % (page+1, page_count)
                else:
                    page_string = ''
                await ctx.message.channel.send(page_string, embed=card)

        self.release_locks()


    async def get_event_embed(self, ctx, timestamp):


        # First try to find the event
        match = self.match_history.query('timestamp == @timestamp').copy()
        if len(match) == 0:
            # We couldn't find the event!
            self.raise_error("Couldn't find event with timestamp {}!".format(timestamp))

        # Now that we have the event, we pretty-print
        # If the length is only 1, then this is a singleplayer event.
        if len(match) == 1 or isinstance(match, pd.Series):
            return await self.get_single_player_event_embed(match)
        else:
            return await self.get_match_embed(match)

    async def get_single_player_event_embed(self, event):

        # Probably this is one of those score adjustments.
        # TODO: find a more elegant solution to this, right
        # now we're just hardcoding the method to get embeds 
        # for certain events (namely singleplayer events)

        # We might sometimes get a Series instead of a dataframe.
        # No matter, we'll just take .iloc[0] if it's a dataframe,
        # so we are guaranteed to have a series in the end.
        # Single player events are always only one row.
        if isinstance(event, pd.Series):
            row = event
        else:
            row = event.iloc[0]

        # Lookup human-friendly version of event type
        title = status_map[row['status']]

        # Add content. There aren't any fields here since that
        # tends to create a bunch of clutter, especially on mobile.
        author = self.user_status.loc[row['playerID'], 'name']
        description = author + '\n'
        description += "Value: {} ({} -> {})\n".format(row['value'], round(row['elo']), round(row['new_elo']))
        if row['comment'] is not None:
            description += row['comment']
        embed = discord.Embed(title=title, author=author, description=description, type='rich',
                              timestamp=row['timestamp'])

        # Show eventID
        embed.set_footer(text='#%d' % event['eventID'].iloc[0])
        return embed
        

    async def get_match_embed(self, match):

        # We can set the title to something like 1v1 Match
        if match['team'].nunique() < 2:
            # If there was only one team, output something like 2-player match
            # instead of the awkward "2 match"
            desc_text = len(match) + "-player"
        else:
            # If there were two or more teams, output something like 1v1 match
            desc_text = 'v'.join(match.groupby('team')['playerID'].count().astype(str).tolist())
        desc_text += ' match'

        # Set the title to the comment, if any.
        if match['comment'].iloc[0] is not None:
            title = match['comment'].iloc[0]
        else:
            title = desc_text

        # Add K factor to description
        desc_text += ' (K=%d)' % match['value'].iloc[0]

        # Instantiate embed
        embed = discord.Embed(title=title, description=desc_text, type='rich', timestamp=match['timestamp'].iloc[0])

        # Each team gets one field. This seems to work well on both mobile and desktop.
        for team, team_members in match.groupby('team'):
            field_name = 'Team %s (%s)' % (team, team_members['status'].iloc[0])
            field_value = ''
            for i, t in team_members.iterrows():
                field_value += '*%s* (%d -> %d)\n' % (self.user_status.loc[t['playerID'], 'name'], round(t['elo']), round(t['new_elo']))
            embed.add_field(name=field_name, value=field_value)

        # Show eventID
        embed.set_footer(text='#%d' % match['eventID'].iloc[0])
        return embed


    async def get_player_card(self, ctx, user_id):
        '''Get an Embed describing the player's stats.

        ctx is a context from which we can grab the server and
        the avatar.

        user_id is of the player whose stats are to be shown.
        '''

        # Try to retrieve the row pertaining to this user,
        # or return None.
        if user_id in self.user_status.index:
            uinfo = self.user_status.loc[user_id]
        else:
            return None

        # Try to get the user's avatar, if any.
        try:
            avatar = ctx.guild.get_member(user_id).avatar_url
        except:
            avatar = None

        # Show name, rank, score in title
        title = '%s (%s, %d)' % (uinfo['name'], uinfo['rank'], 
                round(self.get_masked_elo(self.user_status, user_id)))

        # Construct description field
        description = "Wins: %d / Losses: %d / Total: %d\n" % (uinfo['wins'], uinfo['losses'], uinfo['matches_played'])
        description += "Player ID: %s\n" % user_id
        description += "Raw Elo rating: %d\n" % round(self.get_elo(self.user_status, user_id))
        description += "Bonuses: %d\n" % round(uinfo['mask'])

        # Get all matches played
        ids_played = self.match_history.query('playerID == %s' % user_id)['eventID'].tolist()
        ids_played = [str(i) for i in ids_played]
        description += "Events: %s\n" % (', '.join(ids_played))
        embed = discord.Embed(type='rich', description=description, color=int('0x' + uinfo['color'], base=16))
        if avatar:
            embed.set_author(name=title, icon_url=avatar)
        else:
            embed.set_author(name=title)
        return embed


    @commands.command()
    @commands.check(has_admin_perms)
    async def recalculate(self, ctx):
        '''Recalculate elo ratings from scratch.'''
        await self.acquire_locks()
        await self.recalculate_elo(ctx)
        self.release_locks()
        await ctx.message.channel.send('Recalculated elo ratings!')

    @commands.command()
    @commands.check(has_player_perms)
    async def player(self, ctx, *, name=None):
        '''Show a player's Elo profile.

        Players can be searched by the beginning of their name, by mentioning
        them, or by entering their name and discrim, like player1#1234.
        If no search query is present, the caller's (your) player card
        will be shown, if any.

        For example, `elo! player lekro` will display the profile of all
        players whose names start with 'lekro'. 
        '''

        await self.acquire_locks()

        # We may encounter duplicates when using the various methods of searching
        # for players, so we will make a set here.
        uids = set()
        player_cards = []
        if name is not None:

            # Get page number to display. This will be the last part of the name,
            # if any.
            # But we should first check if there is any whitespace...
            # if there is no whitespace, then the name is the entire thing
            # no matter what, even if it's a number!
            spl = name.split()
            if len(spl) <= 1:
                page = 0
            else:
                try:
                    page = int(spl[-1])
                    # In case we did find a page number,
                    # remove that from the name...
                    name = " ".join(spl[:-1])
                except ValueError:
                    page = 0

            # Now, we can add a list of user IDs by checking for 
            # them in various ways...

            # Check mentions in message
            if len(ctx.message.mentions) > 0:
                for mention in ctx.message.mentions:
                    uids.add(mention.id)

            # Check in same way we do for matches
            member = await self.parser.parse_user(ctx, name)
            if member is not None:
                uids.add(member.id)

            # Check by iterating through names in the server...
            for member in ctx.guild.members:
                # Display name (nick or name)
                if member.display_name.lower().startswith(name.lower()):
                    uids.add(member.id)
                # Name (Discord username)
                if member.name.lower().startswith(name.lower()):
                    uids.add(member.id)

        else:
            # The user hasn't passed a name argument
            # Process self
            uids.add(ctx.message.author.id)

        # Get all relevant player cards
        for uid in uids:
            card = await self.get_player_card(ctx, uid)
            if card is not None:
                player_cards.append(card)

        self.release_locks()

        # If we found no players, tell the caller that!
        if len(player_cards) == 0:
            self.raise_error('Couldn\'t find any players!')

        page_size = self.config['max_player_cards']
        # If we find only one page of players, just output them.
        if len(player_cards) <= page_size:
            for card in player_cards:
                await ctx.message.channel.send(embed=card)
        # If we find more than one page, show the page number as well
        else:
            page_count = (len(player_cards) + page_size - 1) / page_size
            if not (0 <= page < page_count):
                self.raise_error("Page index out of range!")
            # Iterate through the player cards only in the page we want...
            for i, card in enumerate(player_cards[page*page_size:(page+1)*page_size]):
                if i==0:
                    page_string = 'Showing page %d of %d of player cards.' % (page+1, page_count)
                else:
                    page_string = ''
                await ctx.message.channel.send(page_string, embed=card)


    @commands.command()
    @commands.check(has_player_perms)
    async def top(self, ctx, n=10, score_type='mask'):
        '''Show the top n players, by masked Elo or raw Elo.
        
        USAGE: top [NTOP] [TYPE]
        where NTOP is the number of top players to display
        and TYPE is the type of score to sort by

        Valid values for TYPE are mask and raw.
        '''
        valid_score_types = ['mask', 'raw']
        if score_type not in valid_score_types:
            raise EloError('Invalid score type requested! Try one of '
                        + ', '.join(valid_score_types))

        await self.acquire_locks()

        # Make sure the input is an integer
        try:
            n = int(n)
        except ValueError:
            self.raise_error('The number of top players to show must be an integer!')

        # Make sure the number is non-negative
        if n < 0:
            self.raise_error('Cannot display a negative number of top players!')

        # Make sure the number doesn't exceed the configurable limit
        if n > self.config['max_top']:
            self.raise_error('Maximum players to display in top rankings is %d!'\
                    % self.config['max_top'])

        ustatus = self.user_status.copy()

        # Figure out how the user wishes to sort this thing
        if score_type == 'mask':
            ustatus['sort'] = ustatus['elo'] + ustatus['mask']
        elif score_type == 'raw':
            ustatus['sort'] = ustatus['elo']
        else:
            raise_error('Internal error! Score type `{}` is undefined!'.format(score_type))

        # Sort descending by that sort column
        topn = ustatus.sort_values('sort', ascending=False).head(n)
        title = 'Top %d Players' % n
        desc = ''
        for i, (uid, uinfo) in enumerate(topn.iterrows()):
            desc += '%d. %s (%s, %d)\n' % (i+1, uinfo['name'], uinfo['rank'], round(uinfo['sort']))
        print(desc)
        embed = discord.Embed(title=title, type='rich', description=desc)

        self.release_locks()
        return await ctx.message.channel.send(embed=embed)

